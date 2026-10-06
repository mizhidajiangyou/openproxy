"""运行期配置服务：环境基线 + 数据库覆盖层，合成成唯一有效配置。

这是 R23 要求的那个「公开 setter 落点」：控制台改设置时**不会**重建任何服务实例，
只是替换本类持有的一个不可变快照。已经拿到旧快照的组件在自己的下一次读之前保持
旧行为，之后自然收敛 —— 所以 setter 之后不需要广播/重连。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from openproxy.config import Overlays, RuntimeConfig, Settings, load_settings
from openproxy.store.db import Database

OVERLAY_KEY = "runtime_overlays"


def encode_overlays(overlays: Overlays) -> str:
    # **逐字段列举，不要用 dataclasses.asdict**：asdict 会把 ClassVar 之外的
    # 所有字段都写进去，于是「加一个覆盖字段」这件事会静默地只改一半 ——
    # 合成时读得到、落库时没写，重启即失效。列举式让漏一个变成 KeyError
    # 而不是「设置悄悄不生效」。
    return json.dumps(
        {
            "require_key": overlays.require_key,
            "upstream_base": overlays.upstream_base,
            "retain_days": overlays.retain_days,
            "daily_token_quota": overlays.daily_token_quota,
            "free_models_only": overlays.free_models_only,
            "inject_stream_usage": overlays.inject_stream_usage,
            "reasoning_effort": overlays.reasoning_effort,
            # **``None`` 必须写成 ``null`` 而不是 ``[]``**：这两个值语义不同 ——
            # ``None`` = 「没设过，回环境变量基线」，``[]``/``()`` = 「明确要空，
            # 即便环境变量里设了也全部直通」。写成 ``[]`` 会让「控制台清空」
            # 悄悄变成「强制清空」，用户设的 ``OPENPROXY_OPENCODE_MODELS`` 被无视，
            # 而界面上看不出任何区别。
            "opencode_models": (
                None if overlays.opencode_models is None
                else list(overlays.opencode_models)
            ),
        },
        ensure_ascii=False,
    )


def decode_overlays(raw: str | None) -> Overlays:
    """把库里的 JSON 读回覆盖层。任何损坏都退化成空覆盖层，而不是启动失败。"""
    if not raw:
        return Overlays()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Overlays()
    if not isinstance(data, dict):
        return Overlays()

    def read_bool(key: str) -> bool | None:
        value = data.get(key)
        return value if isinstance(value, bool) else None

    def read_int(key: str) -> int | None:
        value = data.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def read_str(key: str) -> str | None:
        """读普通字符串字段。**空串归一化成 ``None``**（= 回环境变量基线）。"""
        value = data.get(key)
        return value.strip() or None if isinstance(value, str) else None

    def read_str_or_blank(key: str) -> str | None:
        """读「空串有意义」的字符串字段。

        与 :func:`read_str` 的区别就是本函数存在的全部理由：
        ``reasoning_effort`` 的 ``""`` 表示**「显式关闭强制」**，
        而 ``None`` 表示「没设过，回基线」。如果在这里把 ``""`` 读成
        ``None``，那么「用户在界面关掉开关」这个动作在**重启后就会被撤销**
        —— 覆盖层读回来是「没设过」，于是环境变量里的值复活，
        而界面显示已关闭、没有任何提示。

        这个 bug 极隐蔽：功能在**当次运行**里是对的（覆盖层还热着），
        只有重启后才暴露。所以必须显式区分，不能靠 ``read_str``。
        """
        value = data.get(key)
        if not isinstance(value, str):
            return None
        # 键存在但值是空串/纯空白 -> 返回 ""（显式关闭）；键不存在 -> None（回基线）。
        return value.strip()

    def read_str_tuple(key: str) -> tuple[str, ...] | None:
        """读字符串列表。

        ``[]`` 与「键不存在」要区分：前者是「明确要空列表」（全部直通），
        后者是「没设过」（回环境变量基线）。所以不能写成
        ``tuple(...) or None`` —— 那会把两者都变成 ``None``。
        """
        value = data.get(key)
        if not isinstance(value, list):
            return None
        return tuple(v.strip() for v in value if isinstance(v, str) and v.strip())

    return Overlays(
        require_key=read_bool("require_key"),
        upstream_base=read_str("upstream_base"),
        retain_days=read_int("retain_days"),
        daily_token_quota=read_int("daily_token_quota"),
        free_models_only=read_bool("free_models_only"),
        inject_stream_usage=read_bool("inject_stream_usage"),
        reasoning_effort=read_str_or_blank("reasoning_effort"),
        opencode_models=read_str_tuple("opencode_models"),
    )


class ConfigService:
    """有效配置的唯一定义处。线程安全（读多写少，锁只护写）。"""

    def __init__(self, db: Database, settings: Settings | None = None) -> None:
        self._db = db
        self._settings = settings if settings is not None else load_settings()
        self._lock = threading.Lock()
        stored = decode_overlays(db.kv_get(OVERLAY_KEY))
        self._config = RuntimeConfig.compose(self._settings, stored)

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def snapshot(self) -> RuntimeConfig:
        """当前有效配置。返回一个不可变对象，调用方随便持有。"""
        return self._config

    @property
    def overlays(self) -> Overlays:
        return self._config.overlays

    def apply_overlays(self, overlays: Overlays) -> RuntimeConfig:
        """公开 setter：校验 → 落库 → 重合成。落库失败则配置不变。"""
        validated = overlays.validated()
        self._db.kv_set(OVERLAY_KEY, encode_overlays(validated))
        composed = RuntimeConfig.compose(self._settings, validated)
        with self._lock:
            self._config = composed
        return composed

    def patch(self, **changes: Any) -> RuntimeConfig:
        """只改给定字段，其余保持当前覆盖层。

        读-改-写**整体持锁**。之前是「锁内读、锁外写」，两个并发 patch 会各自拿到
        同一份旧快照，后写的把先写的整个覆盖层顶掉 —— 改 ``require_key`` 会连带
        丢掉刚改的 ``retain_days``。当前单 worker 事件循环上因为这个函数没有
        ``await`` 而碰不到，但那是调度器的性质，不是代码的性质。
        """
        with self._lock:
            current = self._config.overlays
            validated = current.patch(**changes).validated()
            self._db.kv_set(OVERLAY_KEY, encode_overlays(validated))
            self._config = RuntimeConfig.compose(self._settings, validated)
            return self._config

    def reset_overlays(self) -> RuntimeConfig:
        """清空覆盖层，回到环境变量基线。"""
        return self.apply_overlays(Overlays())

    def reload_from_db(self) -> RuntimeConfig:
        """从库里重新读覆盖层（另一个进程改了 DB 时用）。"""
        composed = RuntimeConfig.compose(
            self._settings, decode_overlays(self._db.kv_get(OVERLAY_KEY))
        )
        with self._lock:
            self._config = composed
        return composed
