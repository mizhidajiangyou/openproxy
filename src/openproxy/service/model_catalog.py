"""免费模型目录与上游可用性探测。

``FREE_MODELS``（领域层）是「哪些模型可以转发」的**唯一事实来源**；
本模块只负责补充**它们现在还在不在**这一层动态信息。两者职责不混：
静态清单是策略，探测是观测。

上游 ``/v1/models`` 只返回 id、不带价格，所以不能用它判断「免费」——
那等于把免费策略外包给上游，改一次上游就悄悄改了本站的策略。

## 两种探测，缺一不可
==================================

``/v1/models``（**存在性**）
    一次 GET 拿到全部 id。便宜、快、可重试。但它**查不出站外能不能调通**。

最小 ``chat/completions``（**可达性**）
    必须真发一次请求。实测 2026-10-04：10 个免费模型里**9 个**回
    ``403 FreeTierError``（``OpenCode's free tier can only be used from within
    OpenCode``），加 Referer / Origin / 改 UA 都绕不过，只有 ``space-bunny-free``
    从站外调得通。而 ``/v1/models`` 对这9 个模型照常返回 id —— 所以只看存在性，
    控制台会把「点得通」和「点不通」显示成同一个样子。

代价与边界
----------
可达性探测**每个模型发一次真实请求**，会消耗上游额度（免费档但有配额），
所以：串行、带间隔、启动时只在「距上次探测超过一天」时才跑，且可整个关掉。
网络失败归``unknown`` 而不是 ``blocked`` —— 「 本站连不上」和「上游明确拒绝」
是两件不能混的事，混了会让一次断网把9 个模型误标成不可用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from openproxy.domain import FREE_MODEL_IDS, FREE_MODELS, OBSERVED_EXTERNAL_OK, FreeModel
from openproxy.store.probe_store import Reachability

log = logging.getLogger("openproxy.catalog")

PROBE_PATH: Final = "/v1/models"
CHAT_PATH: Final = "/v1/chat/completions"

#: 探测用的最小请求体。``max_tokens=1`` 让它只够跑通鉴权与路由—— 我们要验的是
#: 「这个模型能不能从站外调」，不是「它答得好不好」。
PROBE_BODY: Final = {
    "model": "",
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 1,
    "stream": False,
}

#: 每次可达性探测之间的间隔。上游有每 IP 配额，10 个模型连发容易被判成滥用。
REACHABILITY_GAP_SECONDS: Final = 1.5

#: 探测结果的有效期。超过它就重新探测 —— 上游的策略会变，本站的观测不能是
#: 一份永远不过期的快照。
REACHABILITY_TTL_MS: Final = 24 * 3600 * 1000

#: 上游拒绝时的识别特征。``FreeTierError`` 是实测值；文案匹配放在类型之后，
#: 因为类型比文案稳定。两者都**只在 4xx 上认**（见 :func:`classify_probe_response`）。
BLOCKED_MARKERS: Final = ("freetiererror", "can only be used from within")

#: 判定时最多读多少正文。够认出标记就行，且不让一个几 MB 的错误页拖慢判定。
MAX_BODY_SNIFF_BYTES: Final = 600


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    model: FreeModel
    available: bool | None
    latency_ms: int | None
    requests: int = 0
    total_tokens: int = 0
    errors: int = 0
    avg_latency_ms: int = 0
    reachability: Reachability | None = None

    def to_public(self) -> dict[str, Any]:
        return {
            **self.model.to_public(self.available, self.latency_ms),
            "requests": self.requests,
            "total_tokens": self.total_tokens,
            "errors": self.errors,
            "avg_latency_ms": self.avg_latency_ms,
            "reachability": (
                None if self.reachability is None else self.reachability.to_public()
            ),
        }


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    latency_ms: int
    available_ids: frozenset[str]
    checked_at: int
    detail: str = ""
    status: int = 0
    reachability: dict[str, Reachability] | None = None
    """模型 id → 站外可达性。``None`` = 本轮没跑可达性探测（开关关着）。"""

    def to_public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
            "detail": self.detail,
            "status": self.status,
            "upstream_count": len(self.available_ids),
            "reachability": None
            if self.reachability is None
            else {k: v.to_public() for k, v in self.reachability.items()},
        }


def parse_model_ids(payload: Any) -> frozenset[str]:
    """从 ``/v1/models`` 响应里取出 id 集合。任何畸形结构都退化成空集合。"""
    if not isinstance(payload, dict):
        return frozenset()
    data = payload.get("data")
    if not isinstance(data, list):
        return frozenset()
    ids = set()
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.add(item["id"])
    return frozenset(ids)


def classify_probe_response(status_code: int, body: bytes) -> tuple[str, str]:
    """把一次探测响应当成 ``(Reachability.status, detail)``。

    状态码与正文都参与判断 —— 实测 ``403 FreeTierError`` 的正文里写着
    ``can only be used from within OpenCode``，只认状态码会把「上游临时 503」
    与「上游明确不许你用」混成同一类。

    **文案标记只在 4xx 上认**：否则一个 500 的响应体里恰好出现了那串字（比如
    上游网关把自己的错误页包在那段文字里），就会被记成「上游明确拒绝」——
    而 5xx 的真实含义是「上游自己出问题了」，两者的处置完全不同。
    """
    if 200 <= status_code < 300:
        return "ok", ""
    if 400 <= status_code < 500:
        lowered = body[:MAX_BODY_SNIFF_BYTES].decode("utf-8", errors="replace").lower()
        if any(marker in lowered for marker in BLOCKED_MARKERS):
            return "blocked", "上游免费额度只允许在 OpenCode 客户端内使用"
        if status_code in {401, 403}:
            return "blocked", f"上游拒绝（HTTP {status_code}）"
    return "unknown", f"上游返回 {status_code}"


class ModelCatalog:
    """静态清单 + 上次探测结果的合并视图。"""

    def __init__(self, *, store: Any | None = None) -> None:
        self._last: ProbeResult | None = None
        self._store = store
        self._reach: dict[str, Reachability] = store.load() if store else {}

    @property
    def last_probe(self) -> ProbeResult | None:
        return self._last

    @property
    def reachability(self) -> dict[str, Reachability]:
        return dict(self._reach)

    def reachability_of(self, model_id: str) -> Reachability:
        """某个模型的站外可达性。没探测过就是 ``unknown`` —— 不是「不可用」。"""
        return self._reach.get(model_id, Reachability(status="unknown"))

    def entries(self, stats: dict[str, dict[str, int]] | None = None) -> list[CatalogEntry]:
        """清单 + 上次探测的可用性 + 区间用量统计。"""
        usage = stats or {}
        available: frozenset[str] | None = (
            self._last.available_ids if self._last and self._last.ok else None
        )
        out: list[CatalogEntry] = []
        for model in FREE_MODELS:
            stat = usage.get(model.model_id, {})
            out.append(
                CatalogEntry(
                    model=model,
                    available=None if available is None else model.model_id in available,
                    latency_ms=self._last.latency_ms if self._last and self._last.ok else None,
                    requests=int(stat.get("requests", 0)),
                    total_tokens=int(stat.get("total_tokens", 0)),
                    errors=int(stat.get("errors", 0)),
                    avg_latency_ms=int(stat.get("avg_latency_ms", 0)),
                    reachability=self._reach.get(model.model_id),
                )
            )
        return out

    def is_allowed(self, model_id: str, *, free_only: bool) -> bool:
        if not free_only:
            return True
        return model_id in FREE_MODEL_IDS

    def catalog_ids(self) -> list[str]:
        return sorted(m.model_id for m in FREE_MODELS)

    def is_stale(self, *, now_ms: int) -> bool:
        """已有的探测结果是否该刷新了。

        没有结果 = 该探测（返回 ``True``）。有结果就看时间 —— 只看**可达性**那一项
        的时间，因为 ``/v1/models`` 的存在性探测在 :meth:`probe` 里每次都会重跑。
        """
        if not self._reach:
            return True
        newest = max((r.checked_at for r in self._reach.values()), default=0)
        return newest <= 0 or (now_ms - newest) >= REACHABILITY_TTL_MS

    async def probe(
        self,
        client: httpx.AsyncClient,
        upstream_base: str,
        *,
        reachability: bool = False,
        gap: float = REACHABILITY_GAP_SECONDS,
    ) -> ProbeResult:
        """向 ``/v1/models`` 发一次 GET 探测在线模型。

        这是**幂等 GET**，所以失败可以安全重试一次；而 ``/v1/chat/completions``
        是有副作用的 POST，中转路径上刻意不重试（重试会白白消耗上游额度，
        且可能重复计费）。

        ``reachability=True`` 时**额外**逐个模型发一次最小 chat 请求，把「站外
        能不能调通」也测出来并落库。默认 ``False``：它消耗额度，不能让任何人
        点一下按钮就十发请求。
        """
        url = upstream_base.rstrip("/") + PROBE_PATH
        checked_at = int(time.time() * 1000)
        last_error = ""
        for _attempt in range(2):
            started = time.monotonic()
            try:
                response = await client.get(url)
            except httpx.TimeoutException:
                last_error = "探测超时"
                continue
            except httpx.HTTPError as exc:
                last_error = f"网络错误: {exc.__class__.__name__}"
                continue
            latency = int((time.monotonic() - started) * 1000)
            if response.status_code >= 400:
                last_error = f"上游返回 {response.status_code}"
                continue
            try:
                ids = parse_model_ids(response.json())
            except ValueError:
                last_error = "上游响应不是合法 JSON"
                continue
            result = ProbeResult(
                ok=True,
                latency_ms=latency,
                available_ids=ids,
                checked_at=checked_at,
                status=response.status_code,
            )
            if reachability:
                # 只测清单内的模型：清单外的 id 本来就不会被转发，测它们是纯浪费。
                targets = [m.model_id for m in FREE_MODELS if m.model_id in ids]
                self._reach = await self.probe_reachability(
                    client, upstream_base, targets, gap=gap
                )
                if self._store is not None:
                    await asyncio.to_thread(self._store.save, self._reach)
                result = ProbeResult(
                    ok=True,
                    latency_ms=latency,
                    available_ids=ids,
                    checked_at=checked_at,
                    status=response.status_code,
                    reachability=dict(self._reach),
                )
            self._last = result
            return result
        failed = ProbeResult(
            ok=False,
            latency_ms=0,
            available_ids=frozenset(),
            checked_at=checked_at,
            detail=last_error or "未知错误",
            reachability=dict(self._reach) if self._reach else None,
        )
        self._last = failed
        return failed

    async def probe_reachability(
        self,
        client: httpx.AsyncClient,
        upstream_base: str,
        model_ids: list[str],
        *,
        gap: float = REACHABILITY_GAP_SECONDS,
    ) -> dict[str, Reachability]:
        """逐个模型发一次最小请求，验站外可达性。**串行**且带间隔。

        串行不是为了礼貌，而是并发请求会被上游按「突发」判定；间隔同理。
        单个模型失败不影响其余 —— 一个模型的网络抖动不该让整轮探测报废。
        """
        url = upstream_base.rstrip("/") + CHAT_PATH
        out: dict[str, Reachability] = {}
        for index, model_id in enumerate(model_ids):
            if index:
                await asyncio.sleep(gap)
            body = dict(PROBE_BODY)
            body["model"] = model_id
            checked_at = int(time.time() * 1000)
            try:
                response = await client.post(url, json=body)
            except httpx.TimeoutException:
                out[model_id] = Reachability(
                    status="unknown", checked_at=checked_at, detail="探测超时"
                )
                continue
            except httpx.HTTPError as exc:
                out[model_id] = Reachability(
                    status="unknown",
                    checked_at=checked_at,
                    detail=f"网络错误: {exc.__class__.__name__}",
                )
                continue
            status, detail = classify_probe_response(response.status_code, response.content)
            out[model_id] = Reachability(
                status=status,
                checked_at=checked_at,
                detail=detail,
                status_code=response.status_code,
            )
        return out

    def observed_hint(self, model_id: str) -> str | None:
        """给界面的一句话提示。

        只在「有实测结论、且与探测结果不冲突」时才说话 —— 探测优先于此处的静态
        观测值（那条是2026-10-04 的一次快照，上游随时可能放开）。
        """
        reach = self._reach.get(model_id)
        if reach is not None and reach.status in {"ok", "blocked"}:
            return None
        if model_id in OBSERVED_EXTERNAL_OK:
            return "实测站外可调"
        return None
