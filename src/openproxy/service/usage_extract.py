"""从上游响应里抽 token 用量，以及从请求体里抽路由元信息。

**为什么这里必须容忍一切畸形输入**（E-009 / R07）：

* 非流式响应体可能不是 JSON（上游网关页、HTML 错误页、被截断的 JSON）；
* 流式响应按任意字节边界切块，一条 SSE 行可能横跨三块；
* ``usage`` 在流式里默认是 ``null``，只有注入 ``stream_options.include_usage``
  才会出现一个带数字的 usage 帧；
* 有些端点只给 ``prompt_tokens`` / ``completion_tokens``，不给 ``total_tokens``。

所以策略是：**抽不到就明确标成 unknown，绝不用「0 token」冒充「没有用量」**。
``TokenUsage.known`` 就是这个区分；统计层把它聚合成 ``unknown_usage``，界面显式提示
「有 N 次调用没拿到用量」，而不是让总量悄悄少算。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from openproxy.domain import VALID_REASONING_EFFORTS, TokenUsage

#: 单条 SSE 行超过这个长度就丢弃缓冲并记一次超长。防止上游（或恶意客户端）
#: 发一条几 MB 的单行把内存吃光。
MAX_SSE_LINE_BYTES: Final = 1 << 20

#: 顶层直读的字段。
_FLAT_FIELDS: Final = ("prompt_tokens", "completion_tokens", "total_tokens")

#: 嵌套字段。**真实上游就是这种形状**（实测 opencode Zen）——
#: ``prompt_tokens_details.cached_tokens`` / ``completion_tokens_details.reasoning_tokens``。
#: 只读顶层的话，缓存命中与推理 token 会永远记成 0，「缓存命中率」这类指标全错。
_NESTED_FIELDS: Final = (
    ("cached_tokens", "prompt_tokens_details", "cached_tokens"),
    ("reasoning_tokens", "completion_tokens_details", "reasoning_tokens"),
)

#: 有些上游（含 o200k 风格的端点）会把它们平铺在 ``usage`` 上，所以两处都读。
_ALSO_FLAT: Final = ("cached_tokens", "reasoning_tokens")


def _coerce_int(value: Any) -> int | None:
    """只接受真正的整数（``bool`` 是 ``int`` 子类，要排除）；字符串数字也收。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return None
    return None


def usage_from_mapping(raw: Any) -> TokenUsage | None:
    """从上游的 ``usage`` 对象里读计量。返回 ``None`` 表示「这里没有可用用量」。

    ``None`` 与「全 0 但已知」是两种不同状态，调用方必须区分。

    同时认两种形状：平铺（``usage.cached_tokens``）与 OpenAI 风格的嵌套
    （``usage.prompt_tokens_details.cached_tokens``）。后者是真实上游在用的形状。
    """
    if not isinstance(raw, Mapping):
        return None

    numbers = _collect_token_numbers(raw)
    if not numbers:
        return None

    prompt = numbers.get("prompt_tokens", 0)
    completion = numbers.get("completion_tokens", 0)
    total = numbers.get("total_tokens")
    if total is None and ("prompt_tokens" in numbers or "completion_tokens" in numbers):
        # 上游没给 total 时按 in+out 补，而不是留 0 —— 否则图表上会出现
        # 「分解之和 ≠ 总量」的自相矛盾数字。
        total = prompt + completion

    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=numbers.get("cached_tokens", 0),
        reasoning_tokens=numbers.get("reasoning_tokens", 0),
        total_tokens=total if total is not None else prompt + completion,
        known=True,
    )


def _collect_token_numbers(raw: Mapping[str, Any]) -> dict[str, int]:
    """把 ``usage`` 对象里所有能认出来的 token 数字收成一张表。

    顺序有意义：顶层平铺优先于嵌套 details（两者都给时以平铺为准），
    平铺缺失时才去翻 ``*_details``。
    """
    numbers: dict[str, int] = {}
    for field in (*_FLAT_FIELDS, *_ALSO_FLAT):
        if field in raw:
            parsed = _coerce_int(raw[field])
            if parsed is not None:
                numbers[field] = parsed
    for field, parent, child in _NESTED_FIELDS:
        if field in numbers:
            continue
        container = raw.get(parent)
        if isinstance(container, Mapping) and child in container:
            parsed = _coerce_int(container[child])
            if parsed is not None:
                numbers[field] = parsed
    return numbers


def usage_from_json_body(body: bytes) -> TokenUsage:
    """非流式响应体 → 用量。解析失败返回 ``unknown``，不抛异常。"""
    if not body:
        return TokenUsage.unknown()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return TokenUsage.unknown()
    if not isinstance(payload, Mapping):
        return TokenUsage.unknown()
    found = usage_from_mapping(payload.get("usage"))
    return found if found is not None else TokenUsage.unknown()


def usage_from_sse_event(data_line: str) -> TokenUsage | None:
    """单条 SSE ``data:`` 载荷 → 用量。非对象 / 无 usage 字段返回 ``None``。"""
    text = data_line.strip()
    if not text or text == "[DONE]":
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return usage_from_mapping(payload.get("usage"))


class SseUsageScanner:
    """增量扫描 SSE 字节流，边转发边抽 usage。

    用法：``feed`` 每来一块转发一块，``usage`` 随时可读。**不缓存响应正文**，
    只留当前未闭合的那一行，所以内存占用与响应长度无关。
    """

    __slots__ = ("_buf", "_max_line", "_usage", "complete", "oversized_lines")

    def __init__(self, max_line_bytes: int = MAX_SSE_LINE_BYTES) -> None:
        self._buf = bytearray()
        self._max_line = max(1024, int(max_line_bytes))
        self._usage = TokenUsage.unknown()
        self.oversized_lines = 0
        """因超长被丢弃的行数。>0 表示上游发了异常大的单行。"""
        self.complete = False
        """是否见到 ``data: [DONE]``。"""

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf.extend(chunk)
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            self._handle(line)
        if len(self._buf) > self._max_line:
            self._buf.clear()
            self.oversized_lines += 1

    def _handle(self, raw_line: bytes) -> None:
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            return  # event: / id: / retry: / 注释行，对用量无意义
        payload = line[len(b"data:") :].strip()
        if payload == b"[DONE]":
            self.complete = True
            return
        found = usage_from_sse_event(payload.decode("utf-8", errors="replace"))
        if found is not None:
            self._usage = self._usage.merged_with(found)

    def close(self) -> TokenUsage:
        """流结束：处理残留的半行（上游没发换行就断开的情况）。"""
        if self._buf:
            self._handle(bytes(self._buf))
            self._buf.clear()
        return self._usage


@dataclass(frozen=True, slots=True)
class RequestMeta:
    """从请求体里抽出来的路由信息。只留元数据，不留 messages 正文。"""

    model: str = ""
    stream: bool = False
    messages: int = 0
    is_json: bool = True

    @property
    def has_messages(self) -> bool:
        return self.messages > 0


def parse_request_meta(body: bytes) -> RequestMeta:
    """解析请求体。非 JSON / 缺字段都退化成默认值，绝不抛。"""
    if not body:
        return RequestMeta(is_json=False)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return RequestMeta(is_json=False)
    if not isinstance(payload, Mapping):
        return RequestMeta(is_json=False)
    model = payload.get("model")
    stream = payload.get("stream")
    messages = payload.get("messages")
    return RequestMeta(
        model=model.strip()[:120] if isinstance(model, str) else "",
        stream=bool(stream) if isinstance(stream, bool) else False,
        messages=len(messages) if isinstance(messages, list) else 0,
        is_json=True,
    )


def normalise_model(body: bytes, model: str) -> bytes:
    """把请求体里的 ``model`` 换成归一化后的那个。

    **为什么需要**：白名单判定用的是 :func:`parse_request_meta` 归一化过的
    ``model``（去首尾空白），而转发出去的正文是客户端原样那份。于是
    ``{"model": " space-bunny-free "}`` 能过白名单，转上去却被上游回
    ``401 ModelError`` —— 而那个码的字面意思是「凭证无效」，客户端会误判成密钥
    有问题，正好是 :meth:`ProxyService._guard_model` 想避免的那件事。

    改不动就原样返回：宁可让上游回 401，也不能因为改写失败把用户的请求弄坏
    （与 :func:`inject_stream_usage` 同一原则）。
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict) or payload.get("model") == model:
        return body
    payload["model"] = model
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


def inject_stream_usage(body: bytes) -> bytes:
    """给流式请求注入 ``stream_options.include_usage=true``。

    没有它，上游每一帧的 ``usage`` 都是 ``null``，流式调用**完全无法统计**。
    已经是 true 就原样返回；body 不是 JSON 或改写失败就原样返回（宁可统计不到，
    也不能因为改写失败把用户的请求弄坏）。
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    options = payload.get("stream_options")
    if isinstance(options, dict) and options.get("include_usage") is True:
        return body
    if options is not None and not isinstance(options, dict):
        return body
    merged = dict(options) if isinstance(options, dict) else {}
    merged["include_usage"] = True
    payload["stream_options"] = merged
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


#: 本站实测被上游接受的思考级别。**唯一事实来源在领域层**（配置层要拿它做校验，
#: 而配置层不能反向依赖 service 层），这里只保留别名。两边写歪了会在下面这行炸。
REASONING_EFFORTS: Final = VALID_REASONING_EFFORTS


def inject_reasoning_effort(body: bytes, effort: str) -> bytes:
    """强制写入 ``reasoning_effort``。

    与 :func:`inject_stream_usage` / :func:`normalise_model` 同一原则：**改不动就
    原样返回**。这里多一条理由 —— 强行写一个上游不认的值会让请求从 200 变成 400，
    那是把「本站配置错了」变成「用户的请求坏了」。

    客户端自己已经写了 ``reasoning_effort`` 时**照样覆写**：这个函数的语义就是
    「本站强制」，不是「本站兜底」。想要兜底行为就把开关关掉。
    """
    if effort not in REASONING_EFFORTS:
        return body
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    if payload.get("reasoning_effort") == effort:
        return body
    payload["reasoning_effort"] = effort
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body
