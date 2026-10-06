"""领域层：与存储、传输、HTTP 都无关的纯数据结构与错误类型。

这一层不 import 任何 stdlib 之外的东西，也不 import 任何 store / service 模块，
所以它可以被所有层安全依赖，是依赖图的叶子。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "FREE_MODELS",
    "ApiKey",
    "ErrorKind",
    "FreeModel",
    "KeyStat",
    "ModelStat",
    "ProxyRejection",
    "StatusPoint",
    "Summary",
    "TokenUsage",
    "TrendPoint",
    "UsageFilter",
    "UsagePage",
    "UsageRecord",
    "freeze_model",
]


class ErrorKind(StrEnum):
    """一次调用为什么没拿到正常结果。用于聚合失败率和定位问题。"""

    NONE = ""
    CLIENT_DISCONNECT = "client_disconnect"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    UPSTREAM_STATUS = "upstream_status"
    REQUEST_TOO_LARGE = "request_too_large"
    BAD_REQUEST = "bad_request"
    NOT_JSON = "not_json"
    AUTH_FAILED = "auth_failed"
    QUOTA_EXCEEDED = "quota_exceeded"
    MODEL_NOT_ALLOWED = "model_not_allowed"
    INTERNAL = "internal"


class ProxyRejection(Exception):
    """在转发**之前**就被本站拒绝。``status`` 是要回给客户端的 HTTP 码。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    def to_payload(self) -> dict[str, Any]:
        return {"error": {"type": self.code, "message": self.message}}


@dataclass(frozen=True, slots=True)
class FreeModel:
    """一个可转发的免费模型。

    ``context_window`` / ``max_output_tokens`` / ``reasoning`` 是**模型能力上限**，
    数据来自 models.dev（opencode Zen 自己的模型元数据源，也是 OpenCode TUI 读的那
    份），由一次 code review 落进静态清单 —— 上游 ``/v1/models`` 只回
    ``id/created/owned_by``，不带这些字段，指望运行时探测等于把「这个模型有多强」
    交给一次网络往返去回答。

    **这三个字段描述的是模型，不是端点**：上游可能给更小的额度，也可能额外限制
    别的参数。界面上必须如实写成「上下文上限」而不是「最大上下文」，否则用户会
    按它去发超长请求然后拿到一个来源不明的 400。
    """

    model_id: str
    display_name: str
    vendor: str
    note: str = ""
    context_window: int = 0
    """上下文上限（token）。0 = 未知，不要显示成 0。"""
    max_output_tokens: int = 0
    """单次回复的最大输出（token）。"""
    reasoning: bool = False
    """是否接受 ``reasoning_effort``。实测 low/medium/high 都接受，
    none/minimal 不认（返回 200 但 usage 为空）。"""

    def to_public(
        self, available: bool | None = None, latency_ms: int | None = None
    ) -> dict[str, Any]:
        return {
            "id": self.model_id,
            "name": self.display_name,
            "vendor": self.vendor,
            "note": self.note,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "reasoning": self.reasoning,
            "available": available,
            "latency_ms": latency_ms,
        }


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一次调用的 token 计量。

    ``known=False`` 表示上游没给用量字段（流式且未注入 ``include_usage``、
    或上游返回了非 JSON）。此时所有数字是 0，**不是**「用量为零」——
    统计层据此把「未知」和「真的没用」区分开，而不是悄悄少算。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    known: bool = False

    @classmethod
    def unknown(cls) -> TokenUsage:
        return cls()

    def merged_with(self, other: TokenUsage) -> TokenUsage:
        """流式分片里可能多次出现 usage，取字段最大值合并。

        ``known`` 是或关系：只要有一片报了用量，整条记录就是已知的。
        """
        if not other.known:
            return self
        if not self.known:
            return other
        return TokenUsage(
            prompt_tokens=max(self.prompt_tokens, other.prompt_tokens),
            completion_tokens=max(self.completion_tokens, other.completion_tokens),
            cached_tokens=max(self.cached_tokens, other.cached_tokens),
            reasoning_tokens=max(self.reasoning_tokens, other.reasoning_tokens),
            total_tokens=max(self.total_tokens, other.total_tokens),
            known=True,
        )


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """一次已完成的调用。**只含计数，不含 prompt / completion 正文。**"""

    ts: int
    model: str
    path: str
    stream: bool
    status: int
    latency_ms: int
    usage: TokenUsage = field(default_factory=TokenUsage.unknown)
    key_id: str | None = None
    key_label: str = ""
    anonymous: bool = True
    bytes_in: int = 0
    bytes_out: int = 0
    error_kind: ErrorKind = ErrorKind.NONE
    client_ip: str = ""


@dataclass(frozen=True, slots=True)
class ApiKey:
    """一张已签发的下游密钥。``id`` 是明文密钥的 sha256 前 32 位十六进制。"""

    id: str
    name: str
    prefix: str
    created_at: int
    note: str = ""
    disabled_at: int | None = None
    daily_token_quota: int | None = None
    last_used_at: int | None = None

    @property
    def disabled(self) -> bool:
        return self.disabled_at is not None

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "created_at": self.created_at,
            "note": self.note,
            "disabled": self.disabled,
            "daily_token_quota": self.daily_token_quota,
            "last_used_at": self.last_used_at,
        }


@dataclass(frozen=True, slots=True)
class UsageFilter:
    """调用记录列表的过滤条件。``None`` 表示不限。"""

    since: int | None = None
    until: int | None = None
    model: str | None = None
    key_id: str | None = None
    anonymous_only: bool = False
    status: str = ""
    """``""`` / ``"ok"``（2xx）/ ``"error"``（非 2xx）。"""
    search: str = ""
    page: int = 1
    page_size: int = 20

    def normalized(self) -> UsageFilter:
        """把外部输入夹到安全区间。

        上界不是洁癖：``page`` 决定 ``offset = (page-1) * page_size``，
        ``since`` / ``until`` 直接进 sqlite3 —— 超出 int64 会抛
        ``OverflowError``，而查询参数里的异常会变成 HTTP 500。在这一层（而不是
        每个路由的 ``Query(...)``）夹一次，是为了让**所有**调用方都受保护。
        """
        page = min(MAX_PAGE, max(1, self.page))
        size = min(MAX_PAGE_SIZE, max(1, self.page_size))
        status = self.status if self.status in {"ok", "error"} else ""
        return UsageFilter(
            since=_clamp_ts(self.since),
            until=_clamp_ts(self.until),
            model=self.model or None,
            key_id=self.key_id or None,
            anonymous_only=bool(self.anonymous_only),
            status=status,
            search=self.search.strip()[:200],
            page=page,
            page_size=size,
        )

    def offset(self) -> int:
        return (self.page - 1) * self.page_size


#: epoch 毫秒的可用区间（int64 能安全表示的毫秒数）。上界取 9999-12-31。
MAX_TS_MS = 253_402_300_799_999
MIN_TS_MS = MIN_INT64_SAFE_MS = -62_135_596_800_000
MAX_PAGE = 1_000_000
MAX_PAGE_SIZE = 200


def _clamp_ts(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(MIN_TS_MS, min(MAX_TS_MS, value))


@dataclass(frozen=True, slots=True)
class UsagePage:
    items: tuple[UsageRecord, ...]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return max(1, -(-self.total // self.page_size))


@dataclass(frozen=True, slots=True)
class Summary:
    """一段时间内的总量。"""

    requests: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    unknown_usage: int = 0
    """上游没报用量的调用次数。>0 说明统计不完整，UI 要显式提示。"""
    avg_latency_ms: int = 0
    bytes_out: int = 0

    def to_public(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "errors": self.errors,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "unknown_usage": self.unknown_usage,
            "avg_latency_ms": self.avg_latency_ms,
            "bytes_out": self.bytes_out,
            "error_rate": round(self.errors / self.requests, 4) if self.requests else 0.0,
            "rpm": 0,
            "tpm": 0,
        }


@dataclass(frozen=True, slots=True)
class TrendPoint:
    bucket: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def to_public(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class ModelStat:
    model: str
    requests: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    errors: int
    avg_latency_ms: int

    def to_public(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "requests": self.requests,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "errors": self.errors,
            "avg_latency_ms": self.avg_latency_ms,
        }


@dataclass(frozen=True, slots=True)
class KeyStat:
    key_id: str
    label: str
    anonymous: bool
    requests: int
    total_tokens: int

    def to_public(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "label": self.label,
            "anonymous": self.anonymous,
            "requests": self.requests,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class StatusPoint:
    bucket: str
    ok: int
    error: int

    def to_public(self) -> dict[str, Any]:
        return {"bucket": self.bucket, "ok": self.ok, "error": self.error}


# ------------------------------------------------------------- 免费模型目录 ---
# 静态清单是「免费」这一契约的唯一事实来源：上游 `/v1/models` 只列 id，不带价格，
# 用它来判断免费与否等于把策略外包给上游。清单变更走一次 code review。
#
# 上下文 / 输出上限 / 思考级别来自 models.dev 的 opencode provider（实测核对过
# 2026-10-04），**不是** /v1/models 给的 —— 那个接口只有 id/created/owned_by。
# 第三方目录站（freellm / pi.dev 等）对同一模型给出的数字互相矛盾（space-bunny-free
# 有 8K / 200K / 1M 三种说法），所以只采信 opencode 自己的数据源。
FREE_MODELS: Final[tuple[FreeModel, ...]] = (
    FreeModel("big-pickle", "Big Pickle", "opencode", "通用旗舰档",
              context_window=200_000, max_output_tokens=32_000, reasoning=True),
    FreeModel("fledge-alpha-free", "Fledge Alpha", "opencode", "alpha 档",
              context_window=1_048_576, max_output_tokens=131_072, reasoning=True),
    FreeModel("ling-3.0-flash-fin-free", "Ling 3.0 Flash Fin", "opencode", "轻量快答",
              context_window=262_144, max_output_tokens=32_768, reasoning=True),
    FreeModel("ling-3.1-flash-free", "Ling 3.1 Flash", "opencode", "轻量快答",
              context_window=262_144, max_output_tokens=32_768, reasoning=True),
    FreeModel("longcat-2.5-preview-free", "LongCat 2.5 Preview", "opencode", "长上下文预览档",
              context_window=1_000_000, max_output_tokens=131_072, reasoning=True),
    FreeModel("mimo-v2.6-flash-free", "MiMo-V2.6-Flash", "opencode", "低延迟档",
              context_window=200_000, max_output_tokens=32_000, reasoning=True),
    FreeModel("muse-spark-1.3-contributor-free", "Muse Spark 1.3", "opencode", "社区贡献档",
              context_window=1_048_576, max_output_tokens=131_072, reasoning=True),
    FreeModel("nemotron-3-ultra-free", "Nemotron 3 Ultra", "opencode", "推理档",
              context_window=1_000_000, max_output_tokens=128_000, reasoning=True),
    FreeModel("nemotron-3.5-lightning-free", "Nemotron 3.5 Lightning", "opencode", "极速推理档",
              context_window=262_144, max_output_tokens=262_144, reasoning=True),
    FreeModel("space-bunny-free", "Space Bunny", "opencode", "默认推荐档",
              context_window=1_048_576, max_output_tokens=524_288, reasoning=True),
)

#: 本站实测**站外可调**的模型（2026-10-04）。
#:
#: 上游 10 个免费模型里有 9 个回``403 FreeTierError``，报文是
#: ``OpenCode's free tier can only be used from within OpenCode``；加Referer /
#: Origin / 改 UA 都绕不过，只有 space-bunny-free 能从站外调通。
#:
#: **这只是2026-10-04 的观察，不是策略承诺** —— 上游随时可能放开或收紧。所以它
#: 只作为「默认提示」，真实判定由 :mod:`openproxy.service.model_catalog` 的探测
#: 结果（落库）覆盖，别把这份清单当权威。
OBSERVED_EXTERNAL_OK: Final[frozenset[str]] = frozenset({"space-bunny-free"})

FREE_MODEL_IDS: Final[frozenset[str]] = frozenset(m.model_id for m in FREE_MODELS)

ANONYMOUS_LABEL: Final = "未署名"
"""没有有效密钥的调用在报表里的桶名。

刻意把「未署名」和「没有数据」分开：开启鉴权前的历史用量、以及之后忘记
配密钥的客户端，都需要看得见，否则「这个客户今天用了多少」答不出来。
"""

#: 本站实测被上游接受的思考级别（2026-10-04，对 space-bunny-free 实测）。
#:
#: ``none`` 与 ``minimal`` **刻意不在这张表里**：上游对它们返回 200，但``usage``
#: 是空的 —— 也就是说这两个值等于「静默关掉思考」，而不是一种可用的档位。把它们
#: 当成合法配置项只会让人以为思考还开着，而实际上一分钱推理预算都没花。
#:
#: ``max`` 是**实测存在**的第四档（2026-10-04）：同一道``27*43``、``max_tokens=40``，
#: 连跑三次 completion_tokens 是 33/ 34 / 33（逼近上限），而 ``high`` 是 29 / 33。
#: 也就是说 ``max`` 确实把推理预算拉得更满，不是被静默忽略的别名。
#: 之所以之前没列：那张表是在只试到``high`` 时写下的。
#:
#: 定义在领域层而不是 service 层：``config.py`` 要用它做校验，而配置层**不能**
#: 反向依赖 service 层（那会让依赖图成环）。
VALID_REASONING_EFFORTS: Final[tuple[str, ...]] = ("low", "medium", "high", "max")

OVERLAY_KEY: Final = "runtime_overlays"


def freeze_model(model_id: str) -> FreeModel | None:
    return next((m for m in FREE_MODELS if m.model_id == model_id), None)


__all__ += [
    "ANONYMOUS_LABEL",
    "FREE_MODEL_IDS",
    "OBSERVED_EXTERNAL_OK",
    "OVERLAY_KEY",
    "VALID_REASONING_EFFORTS",
]
