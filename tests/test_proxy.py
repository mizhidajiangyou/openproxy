"""转发层：出站头、分帧、SSE 透传、错误路径、客户端中断。

每条断言都对应一个真实踩过的坑或一个实测过的上游行为，见注释。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Iterator, Mapping

import httpx
import pytest
from fastapi.responses import Response

from openproxy.config import Overlays, RuntimeConfig, Settings
from openproxy.container import Container
from openproxy.domain import ErrorKind, UsageFilter
from openproxy.service.auth import KeyState, ResolvedClient
from openproxy.service.proxy import (
    BODY_METHODS,
    CallContext,
    _raw_path,
    _TeeStream,
    build_response,
    build_upstream_headers,
    filter_response_headers,
    rejection_kind,
)
from openproxy.service.usage_extract import SseUsageScanner
from openproxy.store import UsageStore
from tests.support.asgi import as_stream, collect, json_request, make_request, take_first
from tests.support.upstream import (
    NO_USAGE,
    AsyncChunkStream,
    FakeUpstream,
    RaisingStream,
    json_response,
    sse_chunks,
    sse_response,
    standard_upstream,
)


def config(**kw: object) -> RuntimeConfig:
    return RuntimeConfig.compose(Settings(**kw))  # type: ignore[arg-type]


#: 本文件里 ``build()`` 建出来的容器；用例结束时统一关。
#:
#: 为什么需要：``Container.build`` 会开一个写线程 + 一个 SQLite 连接，而
#: **Python 3.14 的 ``sqlite3`` 会为未关闭的连接发 ``ResourceWarning``**，本项目
#: ``filterwarnings = error``。这个文件有 30 多处 ``build(...)`` 且多数用例没写
#: ``shutdown()``；漏关的后果不是「这个用例挂了」，而是**失败随机漂移到别的用例**
#: （GC 在哪个用例期间回收就记到哪个头上，实测连跑三次漂了两次）——
#: 漂移失败比稳定失败难查得多。
#:
#: 顺序已实测：普通夹具先拆、autouse 后拆（``EVENTS: ['bb', 'autouse-aa']``），
#: 所以这里在autouse 夹具里关库不会影响还在用的 ``container`` 夹具。
_BUILT: list[Container] = []


@pytest.fixture(autouse=True)
def _close_built_containers() -> Iterator[None]:
    """用例结束时关掉本文件所有 ``build()`` 出来的容器。"""
    _BUILT.clear()
    yield
    while _BUILT:
        c = _BUILT.pop()
        with contextlib.suppress(Exception):
            c.recorder.close()
        with contextlib.suppress(Exception):
            c.database.close()


def build(settings: Settings, transport: httpx.AsyncBaseTransport) -> Container:
    c = Container.build(settings, transport=transport, tz_offset_minutes=480)
    _BUILT.append(c)
    return c


def body_of(response: object) -> bytes:
    raw = getattr(response, "body", None)
    return bytes(raw) if raw is not None else b""


# --------------------------------------------------------- 纯函数：出站头 ---


class TestUpstreamHeaders:
    def test_ua_is_always_forced(self) -> None:
        """Cloudflare 回 ``403 error code: 1010`` 给 ``Python-urllib/*`` 和缺失 UA 的
        请求（实测），所以 UA 必须无条件覆写，不能透传客户端的。"""
        cfg = config(upstream_user_agent="openproxy/1.0")
        out = build_upstream_headers({"user-agent": "Python-urllib/3.14"}, cfg)
        assert out == {"user-agent": "openproxy/1.0"}

    def test_ua_is_set_even_when_client_sends_none(self) -> None:
        assert "user-agent" in build_upstream_headers({}, config())

    def test_client_credentials_are_never_forwarded(self) -> None:
        """本站存在的全部理由：占位凭证碰到上游会回 401 AuthError。"""
        headers: Mapping[str, str] = {
            "Authorization": "Bearer sk-fake",
            "X-Api-Key": "sk-fake",
            "Api-Key": "sk-fake",
            "Content-Type": "application/json",
        }
        out = build_upstream_headers(headers, config())
        assert not [k for k in out if k.lower() in {"authorization", "x-api-key", "api-key"}]
        assert out["Content-Type"] == "application/json"

    def test_credentials_stripped_regardless_of_header_case(self) -> None:
        out = build_upstream_headers({"AUTHORIZATION": "Bearer x", "X-API-KEY": "y"}, config())
        assert out == {"user-agent": config().upstream_user_agent}

    @pytest.mark.parametrize("name", ["User-Agent", "USER-AGENT", "uSeR-aGeNt"])
    def test_ua_is_overridden_not_duplicated(self, name: str) -> None:
        """大写头名不能漏出去。

        第一遍过滤按小写名丢掉 ``user-agent``；第二遍（``FORCED_REQUEST_HEADERS``）
        是双保险。少了第二遍，客户端发 ``User-Agent: Python-urllib/*`` 时出站就会
        **同时**带两个 UA（httpx 不会合并大小写不同的同名头），Cloudflare 那条
        1010 规则就会按客户端那个 UA 判定。
        """
        out = build_upstream_headers({name: "Python-urllib/3.14"}, config())
        assert out == {"user-agent": config().upstream_user_agent}
        assert [k for k in out if k.lower() == "user-agent"] == ["user-agent"]

    async def test_upstream_receives_exactly_one_user_agent(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        await container.proxy.handle(
            json_request(
                {"model": "space-bunny-free"},
                headers={"User-Agent": "Python-urllib/3.12"},
            )
        )
        names = [k for k in upstream.last().headers if k.lower() == "user-agent"]
        assert names == ["user-agent"], f"出站带了多个 UA: {names}"
        assert upstream.last().header("user-agent") == (
            container.config.snapshot.upstream_user_agent
        )

    def test_upstream_key_is_injected_when_configured(self) -> None:
        assert build_upstream_headers({}, config(upstream_key="sk-up"))["authorization"] == (
            "Bearer sk-up"
        )

    def test_upstream_key_overrides_client_credential(self) -> None:
        out = build_upstream_headers({"Authorization": "Bearer sk-fake"}, config(upstream_key="sk-up"))
        assert out["authorization"] == "Bearer sk-up"

    def test_hop_by_hop_headers_dropped(self) -> None:
        out = build_upstream_headers(
            {
                "Connection": "keep-alive",
                "Keep-Alive": "timeout=5",
                "Proxy-Authorization": "x",
                "Te": "trailers",
                "Trailer": "x",
                "Transfer-Encoding": "chunked",
                "Upgrade": "h2c",
                "Host": "elsewhere",
                "Proxy-Connection": "x",
            },
            config(),
        )
        assert list(out) == ["user-agent"]

    def test_accept_encoding_dropped_so_body_arrives_uncompressed(self) -> None:
        assert build_upstream_headers({"Accept-Encoding": "gzip"}, config()) == {
            "user-agent": config().upstream_user_agent
        }

    def test_content_length_dropped_because_body_may_be_rewritten(self) -> None:
        assert "Content-Length" not in build_upstream_headers({"Content-Length": "99"}, config())

    def test_ordinary_headers_pass_through(self) -> None:
        out = build_upstream_headers(
            {"Content-Type": "application/json", "X-Trace": "abc", "Accept": "*/*"}, config()
        )
        assert out["X-Trace"] == "abc"
        assert out["Accept"] == "*/*"


class TestResponseHeaders:
    def test_framing_headers_dropped(self) -> None:
        """httpx 已经解了分帧与 gzip；再把上游的声明透传出去，客户端会二次处理。"""
        assert filter_response_headers(
            {"Transfer-Encoding": "chunked", "Content-Encoding": "gzip", "Content-Length": "5"}
        ) == []

    def test_content_type_and_custom_headers_survive(self) -> None:
        out = filter_response_headers({"Content-Type": "application/json", "X-Request-Id": "r1"})
        assert dict(out)["X-Request-Id"] == "r1"

    def test_duplicate_date_and_server_are_dropped(self) -> None:
        """uvicorn 自己也会发这两个头；转发上游的就变成两份 Date（RFC 9110 §6.6.1 禁止）。"""
        out = dict(
            filter_response_headers(
                {"Date": "Mon, 01 Jan 2026 00:00:00 GMT", "Server": "cloudflare",
                 "X-Keep": "1"}
            )
        )
        assert "Date" not in out and "Server" not in out
        assert out["X-Keep"] == "1"

    def test_repeated_set_cookie_are_not_merged(self) -> None:
        """``Headers.items()`` 会把两个 Set-Cookie 合成一个，客户端读到的
        第一个 cookie 的值直接是坏的。必须用 multi_items。"""
        headers = httpx.Headers([("set-cookie", "s1=1"), ("set-cookie", "s2=2")])
        cookies = [v for k, v in filter_response_headers(headers) if k.lower() == "set-cookie"]
        assert cookies == ["s1=1", "s2=2"]

    def test_repeated_headers_survive_as_separate_entries(self) -> None:
        headers = httpx.Headers([("vary", "accept"), ("vary", "origin")])
        assert len([1 for k, _ in filter_response_headers(headers) if k.lower() == "vary"]) == 2


class TestRejectionKind:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("request_too_large", "request_too_large"),
            ("bad_request", "bad_request"),
            ("model_not_allowed", "model_not_allowed"),
            ("model_required", "bad_request"),
            ("daily_quota_exceeded", "quota_exceeded"),
            ("global_quota_exceeded", "quota_exceeded"),
            ("missing_api_key", "auth_failed"),
            ("invalid_api_key", "auth_failed"),
            ("api_key_disabled", "auth_failed"),
            ("something_new", "auth_failed"),
        ],
    )
    def test_mapping(self, code: str, expected: str) -> None:
        assert rejection_kind(code) == expected

    def test_every_declared_code_has_a_mapping(self) -> None:
        """新增拒绝码却忘了加映射，会静默落进 auth_failed，渠道页的失败原因
        拆解就会说错话。"""
        from openproxy.service.proxy import _REJECTION_KINDS

        for code in ("request_too_large", "bad_request", "model_required",
                     "model_not_allowed", "daily_quota_exceeded",
                     "global_quota_exceeded", "missing_api_key",
                     "invalid_api_key", "api_key_disabled"):
            assert code in _REJECTION_KINDS, f"{code} 没有映射"

    def test_body_methods_include_the_verbs_that_carry_json(self) -> None:
        assert {"POST", "PUT", "PATCH"} <= BODY_METHODS
        assert "GET" not in BODY_METHODS


# ---------------------------------------------------------------- 转发 ---


@pytest.fixture
def upstream() -> FakeUpstream:
    return standard_upstream()


@pytest.fixture
def container(settings: Settings, upstream: FakeUpstream) -> Iterator[Container]:
    c = build(settings, upstream)
    yield c
    c.recorder.close()
    c.database.close()


class TestNonStreamingRelay:
    async def test_response_passes_through_untouched(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        response = await container.proxy.handle(json_request({"model": "space-bunny-free"}))
        assert response.status_code == 200
        assert json.loads(body_of(response))["usage"]["total_tokens"] == 164

    async def test_upstream_sees_forced_ua_and_no_client_credential(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        await container.proxy.handle(
            json_request(
                {"model": "space-bunny-free"},
                headers={"Authorization": "Bearer sk-fake", "User-Agent": "Python-urllib/3.12"},
            )
        )
        last = upstream.last()
        assert last.header("user-agent") == container.config.snapshot.upstream_user_agent
        assert last.header("authorization") is None
        assert last.header("x-api-key") is None

    async def test_body_is_forwarded_byte_for_byte_when_not_streaming(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        await container.proxy.handle(
            json_request({"model": "space-bunny-free", "messages": [{"role": "user", "content": "你好"}]})
        )
        assert json.loads(upstream.last().content)["messages"][0]["content"] == "你好"

    async def test_query_string_is_preserved(self, container: Container, upstream: FakeUpstream) -> None:
        upstream.handlers["/v1/embeddings"] = json_response({"ok": True})
        await container.proxy.handle(
            json_request({"model": "space-bunny-free"}, path="/v1/embeddings", query="beta=true&n=2")
        )
        assert upstream.last().url.endswith("/v1/embeddings?beta=true&n=2")

    async def test_get_without_body_is_not_model_checked(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        """``/v1/models`` 没有请求体；套上模型白名单会把它误杀成 400。"""
        response = await container.proxy.handle(make_request("GET", "/v1/models"))
        assert response.status_code == 200

    async def test_usage_is_recorded(self, container: Container) -> None:
        await container.proxy.handle(json_request({"model": "space-bunny-free"}))
        assert container.recorder.flush(2.0)
        records = container.usage.list(UsageFilter())
        assert records.total == 1
        item = records.items[0]
        assert item.model == "space-bunny-free"
        assert item.status == 200
        assert item.usage.total_tokens == 164
        assert item.usage.cached_tokens == 157  # 来自嵌套的 prompt_tokens_details
        assert item.usage.known is True
        assert item.anonymous is True
        assert item.key_label == "未署名"
        assert item.client_ip == "127.0.0.1"

    async def test_recorded_exactly_once(self, container: Container) -> None:
        await container.proxy.handle(json_request({"model": "space-bunny-free"}))
        await container.proxy.handle(json_request({"model": "space-bunny-free"}))
        assert container.recorder.flush(2.0)
        assert container.usage.list(UsageFilter()).total == 2

    async def test_non_json_response_is_still_forwarded(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        upstream.handlers["/v1/chat/completions"] = httpx.Response(
            200, content=b"<html>hi</html>", headers={"content-type": "text/html"}
        )
        response = await container.proxy.handle(json_request({"model": "space-bunny-free"}))
        assert body_of(response) == b"<html>hi</html>"
        assert container.recorder.flush(2.0)
        assert container.usage.list(UsageFilter()).items[0].usage.known is False

    async def test_stream_flag_injection_off(self, settings: Settings, upstream: FakeUpstream) -> None:
        c = build(Settings(db_path=settings.db_path, inject_stream_usage=False), upstream)
        try:
            await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            assert b"include_usage" not in upstream.last().content
        finally:
            c.recorder.close()
            c.database.close()

    async def test_stream_flag_injection_on(self, container: Container, upstream: FakeUpstream) -> None:
        await container.proxy.handle(json_request({"model": "space-bunny-free", "stream": True}))
        assert json.loads(upstream.last().content)["stream_options"] == {"include_usage": True}


class TestGuards:
    async def test_unknown_model_is_400_not_upstream_401(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        """上游对未知模型回 ``401 ModelError``，字面意思是「凭证无效」；
        本站要拦在前面给 400，否则客户端会误判成密钥问题。"""
        response = await container.proxy.handle(json_request({"model": "gpt-5.6-sol"}))
        assert response.status_code == 400
        payload = json.loads(body_of(response))
        assert payload["error"]["type"] == "model_not_allowed"
        assert "space-bunny-free" in payload["error"]["message"]
        assert not upstream.requests  # 根本没打上游

    async def test_missing_model_field(self, container: Container) -> None:
        response = await container.proxy.handle(json_request({"messages": []}))
        assert response.status_code == 400
        assert json.loads(body_of(response))["error"]["type"] == "model_required"

    async def test_empty_body_on_post(self, container: Container) -> None:
        response = await container.proxy.handle(make_request("POST", body=b""))
        assert response.status_code == 400
        assert json.loads(body_of(response))["error"]["type"] == "bad_request"

    async def test_non_json_body_is_400_model_required(self, container: Container) -> None:
        response = await container.proxy.handle(make_request("POST", body=b"garbage"))
        assert response.status_code == 400

    async def test_oversized_body_is_413(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        c = build(Settings(db_path=settings.db_path, max_body_bytes=1024), upstream)
        try:
            response = await c.proxy.handle(make_request("POST", body=b"x" * 2000))
            assert response.status_code == 413
            assert json.loads(body_of(response))["error"]["type"] == "request_too_large"
            assert c.recorder.flush(2.0)
            record = c.usage.list(UsageFilter()).items[0]
            assert record.error_kind is ErrorKind.REQUEST_TOO_LARGE
        finally:
            c.recorder.close()
            c.database.close()

    async def test_free_models_only_off_lets_anything_through(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        c = build(Settings(db_path=settings.db_path, free_models_only=False), upstream)
        try:
            upstream.handlers["/v1/chat/completions"] = json_response({"model": "gpt-5.6-sol"})
            response = await c.proxy.handle(json_request({"model": "gpt-5.6-sol"}))
            assert response.status_code == 200
        finally:
            c.recorder.close()
            c.database.close()

    async def test_require_key_blocks_anonymous(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        c = build(settings, upstream)
        try:
            c.config.apply_overlays(Overlays(require_key=True))
            response = await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert response.status_code == 401
            assert json.loads(body_of(response))["error"]["type"] == "missing_api_key"
        finally:
            c.recorder.close()
            c.database.close()

    async def test_require_key_accepts_a_issued_key(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        c = build(settings, upstream)
        try:
            _, raw = c.keys.create("甲")
            c.config.apply_overlays(Overlays(require_key=True))
            response = await c.proxy.handle(
                json_request(
                    {"model": "space-bunny-free"},
                    headers={"Authorization": f"Bearer {raw}"},
                )
            )
            assert response.status_code == 200
            assert c.recorder.flush(2.0)
            record = c.usage.list(UsageFilter()).items[0]
            assert record.anonymous is False
            assert record.key_label == "甲"
        finally:
            c.recorder.close()
            c.database.close()

    async def test_quota_blocks_when_exhausted(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        c = build(settings, upstream)
        try:
            # 用签发时返回的**明文**，不能用 key.id —— 库里只有 sha256 前缀
            _key, raw = c.keys.create("甲", daily_token_quota=1)
            headers = {"Authorization": f"Bearer {raw}"}
            first = await c.proxy.handle(
                json_request({"model": "space-bunny-free"}, headers=headers)
            )
            assert first.status_code == 200
            assert c.recorder.flush(2.0)
            assert c.usage.summary().total_tokens >= 1
            second = await c.proxy.handle(
                json_request({"model": "space-bunny-free"}, headers=headers)
            )
            assert second.status_code == 429
            assert json.loads(body_of(second))["error"]["type"] == "daily_quota_exceeded"
        finally:
            c.recorder.close()
            c.database.close()


class TestStreamingRelay:
    async def test_chunks_are_forwarded_verbatim(self, container: Container) -> None:
        chunks = sse_chunks()
        response = await container.proxy.handle(
            json_request({"model": "space-bunny-free", "stream": True})
        )
        assert await collect(response) == b"".join(chunks)

    async def test_streaming_response_headers(
        self, container: Container
    ) -> None:
        response = await container.proxy.handle(
            json_request({"model": "space-bunny-free", "stream": True})
        )
        assert response.headers["content-type"].startswith("text/event-stream")
        # 关掉反向代理的缓冲，否则 token 会被攒成一坨再吐出来
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["cache-control"] == "no-cache"
        assert "transfer-encoding" not in response.headers

    async def test_streaming_usage_is_recorded(self, container: Container) -> None:
        response = await container.proxy.handle(
            json_request({"model": "space-bunny-free", "stream": True})
        )
        await collect(response)
        assert container.recorder.flush(2.0)
        record = container.usage.list(UsageFilter()).items[0]
        assert record.stream is True
        assert record.usage.known is True
        assert record.usage.total_tokens == 167

    async def test_streaming_without_usage_frame_records_unknown(
        self, settings: Settings
    ) -> None:
        up = FakeUpstream(handlers={"/v1/chat/completions": sse_response(sse_chunks(usage=NO_USAGE))})
        c = build(settings, up)
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            await collect(response)
            assert c.recorder.flush(2.0)
            record = c.usage.list(UsageFilter()).items[0]
            assert record.usage.known is False
            assert record.usage.total_tokens == 0
            assert record.status == 200
        finally:
            c.recorder.close()
            c.database.close()

    async def test_bytes_out_counts_stream_payload(self, container: Container) -> None:
        response = await container.proxy.handle(
            json_request({"model": "space-bunny-free", "stream": True})
        )
        payload = await collect(response)
        container.recorder.flush(2.0)
        assert container.usage.list(UsageFilter()).items[0].bytes_out == len(payload)

    async def test_client_disconnect_is_recorded(self, container: Container) -> None:
        """客户端提前断开时这次调用不能凭空消失，否则速率图会出现无法解释的缺口。"""
        response = await container.proxy.handle(
            json_request({"model": "space-bunny-free", "stream": True})
        )
        payload = await take_first(response)
        assert payload
        assert container.recorder.flush(2.0)
        record = container.usage.list(UsageFilter()).items[0]
        assert record.error_kind is ErrorKind.CLIENT_DISCONNECT
        assert record.bytes_out > 0

    async def test_client_disconnect_releases_the_upstream_response(
        self, settings: Settings
    ) -> None:
        """断连后上游连接必须被归还。

        httpx 只在流**正常读完**时才 ``aclose()``；``GeneratorExit`` /
        ``CancelledError`` 会绕过那一行，连接就永远不回到池子里 —— 一个反复
        断流的客户端会一路吃掉出站连接，直到新转发全部失败。
        """
        seen: list[httpx.Response] = []

        class Spy(FakeUpstream):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                response = await super().handle_async_request(request)
                seen.append(response)
                return response

        c = build(settings, Spy(handlers={"/v1/chat/completions": sse_response(sse_chunks())}))
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            await take_first(response)
            assert seen, "假上游没收到请求"
            assert seen[0].is_closed, "上游响应没被关闭，连接泄漏"
            assert c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).total == 1
        finally:
            c.recorder.close()
            c.database.close()

    async def test_disconnect_before_first_chunk_is_still_recorded(
        self, settings: Settings
    ) -> None:
        """一个字节都没发出去就断开 —— 这次调用同样不能凭空消失。"""
        c = build(settings, FakeUpstream(
            handlers={"/v1/chat/completions": sse_response(sse_chunks())}
        ))
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            await as_stream(response).aclose()
            assert c.recorder.flush(2.0)
            records = c.usage.list(UsageFilter())
            assert records.total == 1, "断连的调用必须留下记录"
            assert records.items[0].error_kind is ErrorKind.CLIENT_DISCONNECT
        finally:
            c.recorder.close()
            c.database.close()

    async def test_closing_the_stream_twice_records_once(
        self, settings: Settings
    ) -> None:
        """``aclose`` 可重复调用（Starlette 收尾 + 测试显式收尾都会调），所以
        「已收尾」这个标记必须真的起作用 —— 否则一次调用会被记成两三条，
        报表上的请求数直接虚高。"""
        c = build(settings, FakeUpstream(
            handlers={"/v1/chat/completions": sse_response(sse_chunks())}
        ))
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            stream = as_stream(response)
            await take_first(response)
            await stream.aclose()
            await stream.aclose()
            await stream.aclose()
            assert c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).total == 1, "重复收尾产生了重复记录"
        finally:
            c.recorder.close()
            c.database.close()

    async def test_iterating_past_the_end_records_once(
        self, settings: Settings
    ) -> None:
        """正常读完（``StopAsyncIteration``）后再次迭代，也不该再记一条。"""
        c = build(settings, FakeUpstream(
            handlers={"/v1/chat/completions": sse_response(sse_chunks())}
        ))
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            assert b"[DONE]" in await collect(response)
            for _ in range(2):
                with pytest.raises(StopAsyncIteration):
                    await as_stream(response).__anext__()
            assert c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).total == 1
            assert c.usage.list(UsageFilter()).items[0].error_kind is ErrorKind.NONE
        finally:
            c.recorder.close()
            c.database.close()

    async def test_tee_settles_its_own_guard(self) -> None:
        """``_TeeStream`` 自己也要幂等。

        ``ProxyService._finish`` 还有第二道 ``ctx.recorded`` 守卫，所以从服务层
        看不到这一层是否有效 —— 直接测类本身。
        """
        calls: list[CallContext] = []
        response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=AsyncChunkStream(sse_chunks()),
        )
        ctx = CallContext(
            started=time.monotonic(), path="/x",
            client=ResolvedClient(KeyState.ANONYMOUS),
        )
        stream = _TeeStream(response, ctx, SseUsageScanner(), calls.append)

        await stream.__anext__()
        await stream.aclose()
        await stream.aclose()
        await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

        assert len(calls) == 1, "重复收尾重复调用了记账回调"
        assert ctx.error_kind is ErrorKind.CLIENT_DISCONNECT
        assert response.is_closed, "上游连接必须被归还"

    async def test_tee_completed_stream_is_not_a_disconnect(self) -> None:
        """回归：``_completed`` 缺失时，正常读完的流会被记成「客户端断开」——
        于是成功率和失败原因拆解全都失真。"""
        calls: list[CallContext] = []
        response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=AsyncChunkStream(sse_chunks()),
        )
        ctx = CallContext(
            started=time.monotonic(), path="/x",
            client=ResolvedClient(KeyState.ANONYMOUS),
        )
        stream = _TeeStream(response, ctx, SseUsageScanner(), calls.append)
        with pytest.raises(StopAsyncIteration):
            while True:
                await stream.__anext__()
        assert len(calls) == 1
        assert ctx.error_kind is ErrorKind.NONE, "正常读完不该算作失败"

    async def test_upstream_error_status_survives_a_client_disconnect(
        self, settings: Settings
    ) -> None:
        """上游本来就 4xx/5xx 时，断连不该把根因覆盖掉。

        否则渠道页的失败原因拆解里，那个上游错误码就永远看不到。
        """
        c = build(settings, FakeUpstream(handlers={
            "/v1/chat/completions": sse_response([b'data: {"error":"slow down"}\n\n'], 429)
        }))
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            await take_first(response)
            assert c.recorder.flush(2.0)
            record = c.usage.list(UsageFilter()).items[0]
            assert record.status == 429
            assert record.error_kind is ErrorKind.UPSTREAM_STATUS
        finally:
            c.recorder.close()
            c.database.close()

    async def test_body_methods_are_exactly_the_those_that_carry_json(self) -> None:
        """精确钉住：把 DELETE/OPTIONS 加进来会让它们也被模型白名单管，
        而那对本站没有意义（这两个动词不携带模型）。"""
        assert {"POST", "PUT", "PATCH"} == BODY_METHODS

    @pytest.mark.parametrize("method", ["GET", "DELETE", "OPTIONS", "HEAD"])
    async def test_non_body_methods_skip_the_model_guard(
        self, container: Container, upstream: FakeUpstream, method: str
    ) -> None:
        """没有请求体的方法不套模型白名单 —— 否则 ``GET /v1/models`` 会被误杀成 400。"""
        upstream.handlers["/v1/whatever"] = json_response({"ok": True})
        response = await container.proxy.handle(make_request(method, "/v1/whatever"))
        assert response.status_code == 200

    async def test_upstream_midstream_failure_is_recorded(
        self, settings: Settings
    ) -> None:
        up = FakeUpstream(
            handlers={
                "/v1/chat/completions": httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=RaisingStream(b'data: {"choices":[],"usage":null}\n\n',
                                         httpx.ReadError("boom")),
                )
            }
        )
        c = build(settings, up)
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            with pytest.raises((httpx.ReadError, httpx.HTTPError)):
                await collect(response)
            assert c.recorder.flush(2.0)
            record = c.usage.list(UsageFilter()).items[0]
            assert record.error_kind is ErrorKind.UPSTREAM_UNREACHABLE
        finally:
            c.recorder.close()
            c.database.close()

    async def test_upstream_midstream_timeout_is_classified_as_timeout(
        self, settings: Settings
    ) -> None:
        """流**中途**超时必须归到 ``upstream_timeout``，不是 ``upstream_unreachable``。

        ``httpx.TimeoutException`` 是 ``HTTPError`` 的子类，所以 except 子句的顺序
        决定归类；调换顺序（或把两个码写成同一个）此前没有任何测试能发现 ——
        非流式的 504 路径走的是 ``handle()`` 里另一处 except，压根碰不到这里。
        """
        up = FakeUpstream(
            handlers={
                "/v1/chat/completions": httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=RaisingStream(
                        b'data: {"choices":[],"usage":null}\n\n',
                        httpx.ReadTimeout("stalled"),
                    ),
                )
            }
        )
        c = build(settings, up)
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            with pytest.raises(httpx.TimeoutException):
                await collect(response)
            assert c.recorder.flush(2.0)
            record = c.usage.list(UsageFilter()).items[0]
            assert record.error_kind is ErrorKind.UPSTREAM_TIMEOUT
            assert record.status == 200, "上游先给了 200，超时是传输层的事"
        finally:
            c.recorder.close()
            c.database.close()

    async def test_stream_error_status_body_forwarded(
        self, settings: Settings
    ) -> None:
        """上游以 SSE content-type 回错误状态（少见但存在）时也要原样透传。"""
        up = FakeUpstream(
            handlers={
                "/v1/chat/completions": httpx.Response(
                    429,
                    headers={"content-type": "text/event-stream"},
                    stream=AsyncChunkStream([b'data: {"error":{"message":"slow down"}}\n\n']),
                )
            }
        )
        c = build(settings, up)
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            assert response.status_code == 429
            assert b"slow down" in await collect(response)
            assert c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).items[0].error_kind is ErrorKind.UPSTREAM_STATUS
        finally:
            c.recorder.close()
            c.database.close()

    async def test_oversized_sse_line_is_noted_but_stream_survives(
        self, settings: Settings
    ) -> None:
        up = FakeUpstream(
            handlers={
                "/v1/chat/completions": sse_response(
                    [*(b'data: {"junk":"' + b"A" * (2 << 20), b'"}\n\n'), *sse_chunks()]
                )
            }
        )
        c = build(settings, up)
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            payload = await collect(response)
            assert b"[DONE]" in payload  # 超长行丢了，但后面的帧照常转发
            assert c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).items[0].usage.known is True
        finally:
            c.recorder.close()
            c.database.close()


class TestRecordedStatus:
    """落库的 ``status`` 必须等于**上游真实返回**的状态码。

    总览的「失败率」卡片、渠道页的趋势图、记录页每一行的成功/失败标签全都吃这个
    字段。之前只断言 ``error_kind``，于是把 ``ctx.status = upstream.status_code``
    改成常量 200 也整套件通过 —— 缓冲型上游错误（401/429/502）会被记成「成功」。
    """

    @pytest.mark.parametrize("status", [400, 401, 429, 502, 503])
    async def test_buffered_upstream_error_keeps_its_status(
        self, settings: Settings, status: int
    ) -> None:
        up = FakeUpstream(
            handlers={
                "/v1/chat/completions": json_response(
                    {"error": {"message": "nope"}}, status=status
                )
            }
        )
        c = build(settings, up)
        try:
            response = await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert response.status_code == status
            assert c.recorder.flush(2.0)
            record = c.usage.list(UsageFilter()).items[0]
            assert record.status == status
            assert record.error_kind is ErrorKind.UPSTREAM_STATUS
            assert c.usage.summary().errors == 1
        finally:
            c.recorder.close()
            c.database.close()

    async def test_buffered_success_keeps_its_status(self, settings: Settings) -> None:
        """对照组：201 也得原样记下来，否则上一条会退化成「只要 >=400 就对」。"""
        up = FakeUpstream(
            handlers={"/v1/chat/completions": json_response({"model": "x"}, status=201)}
        )
        c = build(settings, up)
        try:
            await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).items[0].status == 201
            assert c.usage.summary().errors == 0
        finally:
            c.recorder.close()
            c.database.close()

    async def test_streamed_upstream_error_keeps_its_status(self, settings: Settings) -> None:
        up = FakeUpstream(
            handlers={"/v1/chat/completions": sse_response(sse_chunks(), status=503)}
        )
        c = build(settings, up)
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            await collect(response)
            assert c.recorder.flush(2.0)
            record = c.usage.list(UsageFilter()).items[0]
            assert record.status == 503
            assert record.error_kind is ErrorKind.UPSTREAM_STATUS
        finally:
            c.recorder.close()
            c.database.close()


class TestRawPath:
    """出站 URL 必须用**未解码**的原始路径拼。

    ``scope["path"]`` 是解码过的：``%2f`` 变成结构性的 ``/``，于是 ``/zen`` 前缀
    会被 httpx 的 dot-segment 归一化吃掉（实测出站变成 ``https://opencode.ai/etc``）。
    """

    async def test_encoded_slashes_stay_encoded_outbound(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        c = build(settings, upstream)
        try:
            await c.proxy.handle(
                json_request(
                    {"model": "space-bunny-free"},
                    path="/v1/../etc",
                    raw_path=b"/v1/..%2f..%2fetc",
                )
            )
            sent = upstream.last().url
            assert "/zen/v1/..%2f..%2fetc" in sent
            assert "opencode.ai/etc" not in sent, "路径逃出了 upstream_base 的前缀"
        finally:
            c.recorder.close()
            c.database.close()

    def test_raw_path_is_used_verbatim(self) -> None:
        request = make_request("GET", "/v1/a b", raw_path=b"/v1/a%20b%3Fx=1")
        assert _raw_path(request) == "/v1/a%20b%3Fx=1"

    @pytest.mark.parametrize("raw", [None, b""])
    def test_missing_raw_path_falls_back_to_a_quoted_path(self, raw: bytes | None) -> None:
        """没有 ``raw_path`` 的服务器（部分 ASGI 实现、ASGI 测试客户端）不能炸，
        也不能把空格/非 ASCII 原样塞进 URL —— httpx 会抛 InvalidURL。"""
        request = make_request("GET", "/v1/模型 测试", raw_path=raw)
        if raw is None:
            del request.scope["raw_path"]
        assert _raw_path(request) == "/v1/%E6%A8%A1%E5%9E%8B%20%E6%B5%8B%E8%AF%95"

    async def test_fallback_path_still_reaches_upstream(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        c = build(settings, upstream)
        try:
            await c.proxy.handle(
                json_request(
                    {"model": "space-bunny-free"}, path="/v1/chat/completions", raw_path=b""
                )
            )
            assert upstream.last().url.endswith("/zen/v1/chat/completions")
        finally:
            c.recorder.close()
            c.database.close()


class TestBodyLimitBoundary:
    async def test_body_exactly_at_the_limit_is_accepted(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        """``total > limit`` 是**严格大于**：正好等于上限必须放行。

        把 ``>`` 写成 ``>= limit + 1``（或等价地多算一字节）会让边界请求被误杀，
        而这类请求最容易在客户端截断上传时出现。
        """
        limit = 1024
        c = build(Settings(db_path=settings.db_path, max_body_bytes=limit), upstream)
        try:
            head, tail = '{"model": "space-bunny-free", "pad": "', '"}'
            body = (head + "x" * (limit - len(head) - len(tail)) + tail).encode()
            assert len(body) == limit
            response = await c.proxy.handle(make_request("POST", body=body))
            assert response.status_code == 200
        finally:
            c.recorder.close()
            c.database.close()

    async def test_one_byte_over_the_limit_is_rejected(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        limit = 1024
        c = build(Settings(db_path=settings.db_path, max_body_bytes=limit), upstream)
        try:
            # 正好超一字节。把 ``> limit`` 写成 ``> limit + 1`` 就恰好放行这一档，
            # 所以这一条才是真正卡住边界的断言（下面 1025 字节那档只卡住 ``>=``）。
            head, tail = '{"model": "space-bunny-free", "pad": "', '"}'
            body = (head + "x" * (limit + 1 - len(head) - len(tail)) + tail).encode()
            assert len(body) == limit + 1
            response = await c.proxy.handle(make_request("POST", body=body))
            assert response.status_code == 413
            assert json.loads(body_of(response))["error"]["type"] == "request_too_large"
            assert not upstream.requests, "超限的请求体不该被转发出去"
        finally:
            c.recorder.close()
            c.database.close()

    async def test_well_over_the_limit_is_rejected(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        limit = 1024
        c = build(Settings(db_path=settings.db_path, max_body_bytes=limit), upstream)
        try:
            payload = {"model": "space-bunny-free", "pad": "x" * (limit + 64)}
            body = json.dumps(payload, ensure_ascii=False).encode()
            assert len(body) > limit
            response = await c.proxy.handle(make_request("POST", body=body))
            assert response.status_code == 413
        finally:
            c.recorder.close()
            c.database.close()


class TestResponseHeadersOnTheWire:
    """重复响应头必须活到**客户端能看见的地方**。

    只断言 :func:`filter_response_headers` 的返回值是不够的 —— 上一轮就是这么
    漏掉的：helper 正确地返回了列表，两个调用点却立刻包了一层 ``dict()``，
    重复项又被合并，E1-26 的修复在真实响应上完全失效，而整套件是绿的。
    所以这里断言的是 ``response.raw_headers``（Starlette 真正发出去的东西）。
    """

    @staticmethod
    def _dupe_upstream() -> FakeUpstream:
        return FakeUpstream(
            handlers={
                "/v1/chat/completions": httpx.Response(
                    200,
                    json={"model": "space-bunny-free", "usage": None},
                    headers=[
                        ("content-type", "application/json"),
                        ("set-cookie", "s1=1"),
                        ("set-cookie", "s2=2"),
                        ("www-authenticate", 'Bearer realm="a"'),
                        ("www-authenticate", 'Basic realm="b"'),
                        ("connection", "keep-alive"),
                    ],
                )
            }
        )

    async def test_buffered_response_keeps_both_set_cookies(self, settings: Settings) -> None:
        c = build(settings, self._dupe_upstream())
        try:
            response = await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert response.headers.getlist("set-cookie") == ["s1=1", "s2=2"]
            assert response.headers.getlist("www-authenticate") == [
                'Bearer realm="a"',
                'Basic realm="b"',
            ]
            assert "connection" not in response.headers
            assert response.headers["content-type"].startswith("application/json")
            assert "content-length" in response.headers
        finally:
            c.recorder.close()
            c.database.close()

    async def test_streamed_response_keeps_both_set_cookies(self, settings: Settings) -> None:
        up = FakeUpstream(
            handlers={
                "/v1/chat/completions": httpx.Response(
                    200,
                    headers=[
                        ("content-type", "text/event-stream; charset=utf-8"),
                        ("set-cookie", "s1=1"),
                        ("set-cookie", "s2=2"),
                    ],
                    stream=AsyncChunkStream([b'data: {"choices":[],"usage":null}\n\n']),
                )
            }
        )
        c = build(settings, up)
        try:
            response = await c.proxy.handle(
                json_request({"model": "space-bunny-free", "stream": True})
            )
            assert response.headers.getlist("set-cookie") == ["s1=1", "s2=2"]
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"
            assert response.headers["cache-control"] == "no-cache"
        finally:
            c.recorder.close()
            c.database.close()

    def test_defaults_do_not_override_upstream(self) -> None:
        """``defaults`` 是 setdefault 语义：上游自己发了 cache-control 就不覆盖。"""
        out = build_response(
            Response(status_code=200),
            filter_response_headers(
                httpx.Headers([("cache-control", "max-age=60"), ("x-accel-buffering", "yes")])
            ),
            defaults=[("cache-control", "no-cache"), ("x-accel-buffering", "no")],
        )
        assert out.headers["cache-control"] == "max-age=60"
        assert out.headers["x-accel-buffering"] == "yes"

    def test_defaults_are_added_when_upstream_is_silent(self) -> None:
        out = build_response(
            Response(status_code=200),
            filter_response_headers(httpx.Headers([("x-keep", "1")])),
            defaults=[("cache-control", "no-cache")],
        )
        assert out.headers["cache-control"] == "no-cache"
        assert out.headers["x-keep"] == "1"


class TestModelNormalisation:
    """白名单判定与转发出去的正文必须看**同一个** model id。

    之前判定用 ``" space-bunny-free "`` 归一化后的值（通过），转发的却是客户端
    原样那份 —— 上游于是回 ``401 ModelError``，而那个码的字面意思是「凭证无效」，
    客户端会误判成密钥有问题，正是 ``_guard_model`` 明说要避免的那件事。
    """

    async def test_padded_model_is_normalised_on_the_way_up(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        response = await container.proxy.handle(
            json_request({"model": "  space-bunny-free  ", "messages": []})
        )
        assert response.status_code == 200
        assert json.loads(upstream.last().content)["model"] == "space-bunny-free"

    async def test_streaming_normalisation_and_injection_compose(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        """两次改写要能叠加：先归一化 model，再注入 include_usage。"""
        await container.proxy.handle(
            json_request({"model": " space-bunny-free ", "stream": True})
        )
        payload = json.loads(upstream.last().content)
        assert payload["model"] == "space-bunny-free"
        assert payload["stream_options"] == {"include_usage": True}

    async def test_clean_body_is_forwarded_verbatim(
        self, container: Container, upstream: FakeUpstream
    ) -> None:
        """对照组：干净值不该被无谓地重新序列化（键顺序与空白都保持原样）。"""
        raw = b'{"model":"space-bunny-free","messages":[{"role":"user","content":"hi"}]}'
        await container.proxy.handle(make_request("POST", body=raw))
        assert upstream.last().content == raw


class TestQuotaRunsOffTheEventLoop:
    """开着日配额时，转发热路径不能拿事件循环去跑同步 SQLite 聚合。

    实测当天 20 万条记录时那条 ``SUM`` 要 ~10ms：留在循环里就等于每 10 个并发
    SSE 流被打断一次推进（表现为流式卡顿，不是报错）。
    """

    @staticmethod
    def _with_quota(settings: Settings) -> Container:
        return build(
            Settings(db_path=settings.db_path, daily_token_quota=1), standard_upstream()
        )

    async def test_quota_query_runs_on_another_thread(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._with_quota(settings)
        seen: list[str] = []
        real = UsageStore.tokens_between

        def spy(self: UsageStore, since: int, until: int, key_id: str | None = None) -> int:
            seen.append(threading.current_thread().name)
            return real(self, since, until, key_id)

        monkeypatch.setattr(UsageStore, "tokens_between", spy)
        try:
            await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert seen, "配额判定根本没有跑，配额就不生效"
            assert threading.current_thread().name not in seen, (
                f"配额判定跑在了事件循环线程里: {seen}"
            )
        finally:
            c.recorder.close()
            c.database.close()

    async def test_no_quota_means_no_thread_hop(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没配配额就不该付线程切换的代价 —— 那是对默认站点的纯粹倒退。"""
        c = build(settings, standard_upstream())
        hopped = {"n": 0}
        real_to_thread = asyncio.to_thread

        async def spy(func: object, *args: object, **kwargs: object) -> object:
            hopped["n"] += 1
            return await real_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("openproxy.service.auth.asyncio.to_thread", spy)
        try:
            response = await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert response.status_code == 200
            assert hopped["n"] == 0
        finally:
            c.recorder.close()
            c.database.close()

    async def test_quota_still_blocks_when_exceeded(self, settings: Settings) -> None:
        """搬进线程不改变判定结果。"""
        c = self._with_quota(settings)
        try:
            first = await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert first.status_code == 200
            assert c.recorder.flush(2.0)
            second = await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert second.status_code == 429
            assert json.loads(body_of(second))["error"]["type"] == "global_quota_exceeded"
        finally:
            c.recorder.close()
            c.database.close()


class TestFinishIdempotence:
    async def test_finish_records_only_once(self, settings: Settings, upstream: FakeUpstream) -> None:
        """``_finish`` 是幂等的 —— 正常路径、拒绝路径、流式的 aclose 都可能碰到
        同一个 ctx。重复记账会让「请求数」虚高一倍。"""
        c = build(settings, upstream)
        try:
            await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).total == 1

            # 再走一次会被拒绝的路径，它同样会调 _finish
            response = await c.proxy.handle(json_request({"model": "not-listed"}))
            assert response.status_code == 400
            c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).total == 2

            # 同一个 ctx 上重复调用不应再加一条
            ctx = CallContext(started=0.0, path="/x", client=ResolvedClient(KeyState.ANONYMOUS))
            c.proxy._finish(ctx)
            c.proxy._finish(ctx)
            c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).total == 3
        finally:
            c.recorder.close()
            c.database.close()


class TestUpstreamFailures:
    async def test_connect_error_is_502(
        self, settings: Settings
    ) -> None:
        def boom(_rec: object) -> httpx.Response:
            raise httpx.ConnectError("refused")

        c = build(settings, FakeUpstream(handlers={"*": boom}))
        try:
            response = await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert response.status_code == 502
            assert json.loads(body_of(response))["error"]["type"] == "upstream_unreachable"
            assert c.recorder.flush(2.0)
            record = c.usage.list(UsageFilter()).items[0]
            assert record.error_kind is ErrorKind.UPSTREAM_UNREACHABLE
            assert record.status == 502
        finally:
            c.recorder.close()
            c.database.close()

    async def test_timeout_is_504(self, settings: Settings) -> None:
        def boom(_rec: object) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        c = build(settings, FakeUpstream(handlers={"*": boom}))
        try:
            response = await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert response.status_code == 504
            assert json.loads(body_of(response))["error"]["type"] == "upstream_timeout"
            assert c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).items[0].error_kind is ErrorKind.UPSTREAM_TIMEOUT
        finally:
            c.recorder.close()
            c.database.close()

    async def test_error_detail_does_not_leak_exception_text(self, settings: Settings) -> None:
        """异常文本可能带内部 URL / 文件路径，只回类名。"""
        def boom(_rec: object) -> httpx.Response:
            raise httpx.ConnectError("connection to /Users/someone/.ssh failed")

        c = build(settings, FakeUpstream(handlers={"*": boom}))
        try:
            response = await c.proxy.handle(json_request({"model": "space-bunny-free"}))
            assert b"ssh" not in body_of(response)
        finally:
            c.recorder.close()
            c.database.close()

    async def test_upstream_401_model_error_is_forwarded_verbatim(
        self, settings: Settings
    ) -> None:
        """实测上游对未知模型回 401 + ModelError。这个码必须原样透传。"""
        up = FakeUpstream(
            handlers={
                "/v1/chat/completions": json_response(
                    {"type": "error", "error": {"type": "ModelError", "message": "not supported"}},
                    401,
                )
            }
        )
        c = build(Settings(db_path=settings.db_path, free_models_only=False), up)
        try:
            response = await c.proxy.handle(json_request({"model": "nope"}))
            assert response.status_code == 401
            assert json.loads(body_of(response))["error"]["type"] == "ModelError"
            assert c.recorder.flush(2.0)
            assert c.usage.list(UsageFilter()).items[0].error_kind is ErrorKind.UPSTREAM_STATUS
        finally:
            c.recorder.close()
            c.database.close()
