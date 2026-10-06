"""构造最小 Starlette ``Request`` 的测试辅助。

直接手搓 ASGI scope 很容易写错（body 的 ``more_body``、``client``、header 大小写），
所以集中在这里，别处只调用 :func:`make_request`。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol, cast

from fastapi import Request
from starlette.responses import Response, StreamingResponse

DEFAULT_HEADERS: dict[str, str] = {"content-type": "application/json"}


def make_request(
    method: str = "POST",
    path: str = "/v1/chat/completions",
    *,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
    query: str = "",
    client_host: str = "127.0.0.1",
    raw_path: bytes | None = None,
) -> Request:
    """``raw_path`` 缺省等于 ``path``；显式传字节可以模拟「客户端发的是编码过的路径」。

    uvicorn 会把 ``scope["path"]`` 百分号解码、而 ``scope["raw_path"]`` 保留原始字节，
    两者不一致正是路径逃逸的来源，所以这两个值必须能分别构造。
    """
    raw_headers = DEFAULT_HEADERS if headers is None else dict(headers)
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode() if raw_path is None else raw_path,
        "root_path": "",
        "query_string": query.encode(),
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in raw_headers.items()
        ],
        "client": (client_host, 50000),
        "server": ("testserver", 80),
    }
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def json_request(payload: object, **kw: Any) -> Request:
    return make_request(body=json.dumps(payload).encode(), **kw)


class ByteStream(Protocol):
    """``body_iterator`` 的最小形状：既能逐块读，也能关掉（模拟客户端断开）。"""

    def __aiter__(self) -> AsyncIterator[object]: ...

    async def __anext__(self) -> object: ...

    async def aclose(self) -> None: ...


def as_stream(response: Response) -> ByteStream:
    """取出 ``body_iterator``。流式响应才有；普通响应用 :func:`collect` 就够。"""
    assert isinstance(response, StreamingResponse), f"不是流式响应: {type(response).__name__}"
    iterator: object = response.body_iterator
    return cast(ByteStream, iterator)


def _as_bytes(chunk: object) -> bytes:
    """Starlette 把 ``body_iterator`` 声明成 ``AsyncIterable[str | bytes | memoryview]``，
    而我们自己的生成器只 yield bytes。这里做一次窄化，统一收口，
    免得每个调用点都写一遍 isinstance。"""
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, str):
        return chunk.encode("utf-8")
    if isinstance(chunk, memoryview):
        return chunk.tobytes()
    raise TypeError(f"意外的响应块类型: {type(chunk).__name__}")


async def collect(response: Response) -> bytes:
    """把 StreamingResponse 的所有块收成一个 bytes，模拟客户端读到结束。"""
    if not isinstance(response, StreamingResponse):
        return bytes(response.body)
    chunks: list[bytes] = [_as_bytes(c) async for c in response.body_iterator]
    return b"".join(chunks)


async def take_first(response: Response) -> bytes:
    """只读第一块就关闭生成器 —— 模拟客户端提前断开。"""
    iterator = as_stream(response)
    first = _as_bytes(await iterator.__anext__())
    await iterator.aclose()
    return first
