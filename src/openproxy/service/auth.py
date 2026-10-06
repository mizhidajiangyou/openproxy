"""下游客户端鉴权 + 配额。

默认**免鉴权**：任何本地客户端填任意占位密钥都能过（沿用 demo 的行为，零摩擦）。
开启 ``require_key`` 后，客户端必须带本站签发的 ``sk-op-`` 密钥。

无论是否开启，所有请求都会被归到一个桶里：

* 带有效密钥 → 该密钥的桶（``anonymous=False``）
* 没带 / 带错 / 密钥被禁用 → 匿名桶（``anonymous=True``，标签「未署名」）

**统计上区分「未署名」而不是把两者混为一谈**：开启鉴权前的历史用量和之后忘记
配密钥的客户端，都需要在报表里看得见，否则「这个客户今天用了多少」会答不出来。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from openproxy.config import RuntimeConfig
from openproxy.domain import ANONYMOUS_LABEL, ApiKey, ProxyRejection
from openproxy.store import DAY_MS, KeyStore, UsageStore, day_start_ms, now_ms


class KeyState(StrEnum):
    ANONYMOUS = "anonymous"
    VALID = "valid"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class ResolvedClient:
    """鉴权结果。``record`` 直接喂给 :class:`UsageRecord`。"""

    state: KeyState
    key: ApiKey | None = None

    @property
    def anonymous(self) -> bool:
        return self.state is not KeyState.VALID

    @property
    def key_id(self) -> str | None:
        return None if self.key is None else self.key.id

    @property
    def label(self) -> str:
        if self.key is not None:
            return self.key.name
        return ANONYMOUS_LABEL


def extract_bearer(headers: Mapping[str, str]) -> str:
    """从 ``Authorization`` / ``X-Api-Key`` 里取原始令牌。

    ``headers`` 必须是**大小写不敏感**的映射；Starlette 的 ``Headers`` 满足这一点。
    多个候选头同时出现时以 ``Authorization`` 为准（OpenAI 兼容客户端的标准头）。

    顺序上有个必须注意的细节：**先剥空白再判前缀**。反过来写成先 ``strip()``
    整个头值，那么 ``Authorization: "Bearer "`` 会变成 ``"Bearer"``，前缀判不出来，
    于是把字符串 ``"Bearer"`` 当成令牌拿去查库 —— 一次必然失败的查库，而不是
    干脆地认定为「没带令牌」。
    """
    auth = (headers.get("authorization") or "").strip()
    if auth:
        scheme, _sep, rest = auth.partition(" ")
        if scheme.lower() == "bearer":
            return rest.strip()
        # 有的客户端直接塞裸令牌，不带 Bearer 前缀
        return auth
    for header in ("x-api-key", "api-key"):
        value = (headers.get(header) or "").strip()
        if value:
            return value
    return ""


class AuthService:
    def __init__(self, keys: KeyStore, usage: UsageStore) -> None:
        self._keys = keys
        self._usage = usage

    def resolve(self, headers: Mapping[str, str]) -> ResolvedClient:
        """只判定，不拦截。拦截由 :meth:`enforce` 决定，因为「是否需要密钥」
        取决于运行期配置，而判定逻辑和配置无关（可独立测试）。"""
        raw = extract_bearer(headers)
        if not raw:
            return ResolvedClient(state=KeyState.ANONYMOUS)
        found = self._keys.resolve(raw)
        if found is None:
            return ResolvedClient(state=KeyState.UNKNOWN)
        if found.disabled:
            return ResolvedClient(state=KeyState.DISABLED, key=found)
        return ResolvedClient(state=KeyState.VALID, key=found)

    def enforce(self, client: ResolvedClient, config: RuntimeConfig) -> None:
        """按配置决定是否拦截。抛 :class:`ProxyRejection` 表示拒绝。"""
        if client.state is KeyState.VALID:
            return
        if not config.require_key:
            return
        if client.state is KeyState.ANONYMOUS:
            raise ProxyRejection(
                401,
                "missing_api_key",
                "本站已开启密钥校验，请在 Authorization: Bearer <本站签发的密钥> 中提供",
            )
        if client.state is KeyState.DISABLED:
            raise ProxyRejection(401, "api_key_disabled", "该密钥已被停用")
        raise ProxyRejection(401, "invalid_api_key", "密钥无效")


class QuotaService:
    """日配额判定。

    配额查的是**当天已消耗的 token**，窗口是「本地自然日」而非滚动 24 小时 ——
    用户心智里的「今天」就是自然日。超额返回 429，并把实际用量写进 message，
    这样客户端不用再发一次请求就知道超了多少。
    """

    def __init__(self, usage: UsageStore) -> None:
        self._usage = usage
        self._tz_offset_minutes = usage.tz_offset_minutes

    def _today_window(self, now: int) -> tuple[int, int]:
        start = day_start_ms(now, self._tz_offset_minutes)
        return start, start + DAY_MS

    def needs_query(self, client: ResolvedClient, config: RuntimeConfig) -> bool:
        """这次 :meth:`check` 会不会真的去查库。

        调用方用它决定要不要把判定搬进线程池：没配配额时既不查库、也不该为
        线程切换付代价。别把它写成「总是要查」—— 那是热路径上的一次无谓开销。
        """
        if config.daily_token_quota:
            return True
        return bool(client.key is not None and client.key.daily_token_quota)

    async def acheck(
        self, client: ResolvedClient, config: RuntimeConfig, *, now: int | None = None
    ) -> None:
        """热路径专用：把同步判定搬出事件循环。

        没配配额时**直接**同步返回 —— 匿名流量与未设配额的大多数站点根本不会
        查库，为此付一次线程切换纯属倒退。配了配额才走线程池。
        """
        if not self.needs_query(client, config):
            self.check(client, config, now=now)
            return
        await asyncio.to_thread(self.check, client, config, now=now)

    def check(
        self, client: ResolvedClient, config: RuntimeConfig, *, now: int | None = None
    ) -> None:
        """配额判定（同步）。

        内部是当天窗口的一次 ``SUM`` 聚合，所以转发热路径走 :meth:`acheck`：
        实测当天 20 万条记录时这条 SQL 要 10ms，放在事件循环里等于每 10 个
        并发流就掐断一次推进。
        """
        moment = now if now is not None else now_ms()
        start, end = self._today_window(moment)

        if client.key is not None:
            quota = client.key.daily_token_quota
            if quota:
                used = self._usage.tokens_between(start, end, client.key.id)
                if used >= quota:
                    raise ProxyRejection(
                        429,
                        "daily_quota_exceeded",
                        f"该密钥今日已用 {used} token，达到日配额 {quota}",
                    )

        if config.daily_token_quota:
            used = self._usage.tokens_between(start, end)
            if used >= config.daily_token_quota:
                raise ProxyRejection(
                    429,
                    "global_quota_exceeded",
                    f"全站今日已用 {used} token，达到总配额 {config.daily_token_quota}",
                )

    def snapshot(
        self, client: ResolvedClient, config: RuntimeConfig, *, now: int | None = None
    ) -> dict[str, int | None]:
        """给界面看的配额余量。"""
        moment = now if now is not None else now_ms()
        start, end = self._today_window(moment)
        per_key: int | None = None
        if client.key is not None:
            per_key = self._usage.tokens_between(start, end, client.key.id)
        return {
            "today_tokens": per_key,
            "daily_quota": None if client.key is None else client.key.daily_token_quota,
            "global_today_tokens": self._usage.tokens_between(start, end),
            "global_quota": config.daily_token_quota or None,
        }
