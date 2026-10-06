"""测试用的假上游。

**为什么不能直接用 ``httpx.MockTransport``**：它返回的 ``httpx.Response``
带的是同步字节流，于是 ``client.send(..., stream=True)`` 会在 httpx 内部炸在
``assert isinstance(response.stream, AsyncByteStream)`` —— 也就是说它**根本测不了
流式转发这条主路径**。这里自己实现 ``AsyncByteStream``，让 SSE 分块、SSE 行跨块、
首块延迟、异常中断都能被真实驱动。

**它证明了什么、没证明什么**：它证明了转发链路的分帧、usage 抽取、错误透传、
记账调用都是对的（R17 的「mock 证明接线」）。它**不能**证明真实上游可用 —— 那是
``tests/test_smoke_real.py``（``-m network``）的职责。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import httpx


class AsyncChunkStream(httpx.AsyncByteStream):
    """按给定的分块序列产出字节。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class RaisingStream(httpx.AsyncByteStream):
    """先吐 ``prefix``，然后抛异常。用来测「上游中途断流」。"""

    def __init__(self, prefix: bytes, error: Exception) -> None:
        self._prefix = prefix
        self._error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._prefix:
            yield self._prefix
        raise self._error


@dataclass(slots=True)
class Recorded:
    """一次出站请求的快照，供断言用。"""

    method: str
    url: str
    headers: dict[str, str]
    content: bytes

    def json(self) -> Any:
        return json.loads(self.content)

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


#: 分派项：一个现成的响应，或一个「看请求造响应」的函数。
Handler = "httpx.Response | Callable[[Recorded], httpx.Response]"


@dataclass(slots=True)
class FakeUpstream(httpx.AsyncBaseTransport):
    """按「路径后缀 → 响应」分派的假上游。

    ``handlers`` 的键是路径后缀匹配（``/v1/chat/completions``）或 ``"*"``。
    值可以是 :class:`httpx.Response`，也可以是 ``(request) -> httpx.Response`` 的可调用。
    """

    handlers: dict[str, httpx.Response | Callable[[Recorded], httpx.Response]]
    default: httpx.Response | None = None
    requests: list[Recorded] = field(default_factory=list)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        recorded = Recorded(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            content=request.content,
        )
        self.requests.append(recorded)

        path = request.url.path
        handler = self._resolve(path)
        if handler is None:
            handler = self.default
        if handler is None:
            return httpx.Response(404, json={"error": {"message": f"未注册的路径 {path}"}})

        return handler(recorded) if callable(handler) else handler

    def _resolve(self, path: str) -> httpx.Response | Callable[[Recorded], httpx.Response] | None:
        """三级匹配：精确 → 后缀 → 前缀通配。

        之所以需要后缀匹配：生产环境的出站路径是 ``<upstream_base>/v1/...``，而
        ``upstream_base`` 自带路径段（``https://opencode.ai/zen``），所以假上游
        实际收到的是 ``/zen/v1/models``；测试里写精确的 ``/v1/models`` 匹配不上。
        """
        if path in self.handlers:
            return self.handlers[path]
        for key, candidate in self.handlers.items():
            if key != "*" and not key.endswith("*") and path.endswith(key):
                return candidate
        for key, candidate in self.handlers.items():
            if key.endswith("*") and path.startswith(key[:-1]):
                return candidate
        return self.handlers.get("*")

    def last(self) -> Recorded:
        assert self.requests, "没有任何出站请求"
        return self.requests[-1]


# ----------------------------------------------------------------- 构造器 ---


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def usage_payload(
    *,
    model: str = "space-bunny-free",
    prompt: int = 158,
    completion: int = 6,
    cached: int = 157,
    reasoning: int | None = None,
    with_usage: bool = True,
    content: str = "Hi!",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "cmpl-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if with_usage:
        details: dict[str, Any] = {"cached_tokens": cached}
        if reasoning is not None:
            details["reasoning_tokens"] = reasoning
        payload["usage"] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": details,
        }
    else:
        payload["usage"] = None
    return payload


#: 「不发 usage 帧」的哨兵。用 ``None`` 表示不了这件事，因为 ``None`` 同时是
#: 「用默认用量」的合理输入 —— 早先写成默认值 None，结果「测试无 usage 帧」的那个
#: 用例实际发的是默认 usage，测试通过了但完全没测到想测的东西。
_DEFAULT: Final[object] = object()
"""「用默认用量」的哨兵。不能用 ``None``，因为 ``None`` 要留给「不发 usage 帧」。"""

NO_USAGE: Final[dict[str, Any] | None] = None
"""显式传给 :func:`sse_chunks` 表示**不发** usage 帧。"""


def sse_chunks(
    *,
    model: str = "space-bunny-free",
    tokens: tuple[str, ...] = ("你", "好", "！"),
    usage: Any = _DEFAULT,
    finish: bool = True,
    done: bool = True,
    reasoning: str | None = None,
) -> list[bytes]:
    """拼一串 SSE 分块。

    一帧一块；测跨行/逐字节时由调用方自己再切。``usage=NO_USAGE`` 表示
    **不发** usage 帧（模拟上游没开 ``include_usage``）。
    """
    if usage is _DEFAULT:
        usage = {
            "prompt_tokens": 158,
            "completion_tokens": 9,
            "total_tokens": 167,
            "prompt_tokens_details": {"cached_tokens": 157},
        }
    frames: list[bytes] = []

    def frame(obj: Any) -> bytes:
        return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"

    frames.append(frame({"id": "s", "object": "chat.completion.chunk", "model": model,
                         "choices": [{"index": 0, "delta": {"role": "assistant"}}], "usage": None}))
    if reasoning:
        frames.append(frame({"id": "s", "model": model,
                             "choices": [{"index": 0, "delta": {"reasoning_content": reasoning}}],
                             "usage": None}))
    for token in tokens:
        frames.append(frame({"id": "s", "model": model,
                             "choices": [{"index": 0, "delta": {"content": token}}], "usage": None}))
    if finish:
        frames.append(frame({"id": "s", "model": model,
                             "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}],
                             "usage": None}))
    frames.append(frame({"id": "s", "model": model, "choices": [], "usage": usage}))
    if done:
        frames.append(b"data: [DONE]\n\n")
    return frames


def sse_response(chunks: list[bytes], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        stream=AsyncChunkStream(chunks),
    )


def refuse(_recorded: Recorded) -> httpx.Response:
    """固定的分派项：模拟上游连接被拒。用函数而不是 ``raise`` 是因为
    AsyncBaseTransport 的处理器签名收的是 ``Recorded``，不是 ``httpx.Request``。"""
    raise httpx.ConnectError("refused")


def timeout(_recorded: Recorded) -> httpx.Response:
    raise httpx.ReadTimeout("slow")


def standard_upstream(**overrides: Any) -> FakeUpstream:
    """一套常用分派：``/v1/models`` + 非流式 chat + 流式 chat。"""
    handlers: dict[str, Any] = {
        "/v1/models": json_response(
            {"object": "list", "data": [{"id": "space-bunny-free", "object": "model"}]}
        ),
        "/v1/chat/completions": lambda rec: (
            sse_response(sse_chunks())
            if b'"stream":true' in rec.content or b'"stream": true' in rec.content
            else json_response(usage_payload())
        ),
    }
    handlers.update(overrides)
    return FakeUpstream(handlers=handlers)
