"""``/v1/*`` 透传路由的端到端行为（走完整 ASGI 栈）。

``TestClient`` 这一层比直接调 :class:`ProxyService` 多覆盖三件事：路由匹配顺序、
Starlette 的响应体分帧、以及异常处理器。所以两个层次都要有测试。
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openproxy.config import Settings
from tests.support.upstream import (
    NO_USAGE,
    FakeUpstream,
    json_response,
    refuse,
    sse_chunks,
    sse_response,
    standard_upstream,
)

CHAT = "/v1/chat/completions"


def chat(client: TestClient, **payload: object) -> httpx.Response:
    body: dict[str, object] = {"model": "space-bunny-free", "messages": [{"role": "user", "content": "hi"}]}
    body.update(payload)
    return client.post(CHAT, json=body)


class TestHealth:
    def test_relay_health_does_not_touch_upstream(self, client: TestClient, upstream: FakeUpstream) -> None:
        """上游对未知模型回 401 ModelError；把它透给健康检查会被误读成「本站挂了」。"""
        response = client.get("/v1/__health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["upstream"] == "https://opencode.ai/zen"
        assert upstream.requests == []

    def test_site_health_reports_recorder_state(self, client: TestClient) -> None:
        payload = client.get("/api/health").json()
        assert payload["ok"] is True
        assert payload["recorder_dropped"] == 0

    def test_site_health_surfaces_a_real_drop_count(
        self, settings: Settings
    ) -> None:
        """README 称 ``recorder_dropped`` 是「丢弃数唯一可见处」，那就得真的跟着变。

        只在空闲时断言「== 0」的话，把这里的 ``container.recorder.dropped``
        换成常量 0 整套件照样绿 —— 而那正是「统计在悄悄丢」时运维看不到的唯一信号。
        """
        from openproxy.app import create_app

        app = create_app(settings, transport=standard_upstream(), start_pruner=False,
                         start_prober=False, tz_offset_minutes=480)
        with TestClient(app) as c:
            container = c.app.state.container  # type: ignore[attr-defined]
            container.recorder.dropped = 7
            assert c.get("/api/health").json()["recorder_dropped"] == 7

    def test_relay_health_is_registered_before_the_catch_all(
        self, client: TestClient, upstream: FakeUpstream
    ) -> None:
        """路由顺序回归：``/v1/{path:path}`` 会吃掉 ``__health``。"""
        assert client.get("/v1/__health").status_code == 200
        assert client.get("/v1/models").status_code == 200
        assert {r.url.split("opencode.ai")[-1] for r in upstream.requests} == {"/zen/v1/models"}


class TestNonStreaming:
    def test_happy_path(self, client: TestClient) -> None:
        response = chat(client)
        assert response.status_code == 200
        assert response.json()["usage"]["total_tokens"] == 164

    def test_placeholder_credential_is_swallowed(self, client: TestClient, upstream: FakeUpstream) -> None:
        """客户端的必填占位密钥绝不能碰到上游。"""
        chat(client, **{})
        client.post(
            CHAT,
            json={"model": "space-bunny-free", "messages": []},
            headers={"Authorization": "Bearer sk-fake", "X-Api-Key": "dummy"},
        )
        assert upstream.last().header("authorization") is None

    def test_user_agent_is_overridden(self, client: TestClient, upstream: FakeUpstream) -> None:
        """urllib 默认 UA 会被 Cloudflare 403，所以必须覆写。"""
        client.post(CHAT, json={"model": "space-bunny-free", "messages": []},
                    headers={"User-Agent": "Python-urllib/3.14"})
        assert upstream.last().header("user-agent") == (
            "openproxy/1.0 (+https://github.com/local/openproxy)"
        )

    def test_unlisted_model_is_400(self, client: TestClient, upstream: FakeUpstream) -> None:
        response = client.post(CHAT, json={"model": "gpt-5.6-sol", "messages": []})
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "model_not_allowed"
        assert upstream.requests == []

    def test_upstream_error_body_is_forwarded_verbatim(
        self, settings: Settings, upstream: FakeUpstream
    ) -> None:
        upstream.handlers[CHAT] = json_response(
            {"type": "error", "error": {"type": "ModelError", "message": "not supported"}}, 401
        )
        # 关掉白名单，否则请求在本站就被 400 拦掉，压根到不了上游的 401
        app = _app(settings, upstream, free_models_only=False)
        with TestClient(app) as c:
            response = c.post(CHAT, json={"model": "space-bunny-free", "messages": []})
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "ModelError"

    def test_get_models_passes_through(self, client: TestClient) -> None:
        response = client.get("/v1/models")
        assert response.status_code == 200
        assert response.json()["data"][0]["id"] == "space-bunny-free"


class TestDuplicateResponseHeaders:
    """走完整 ASGI 栈、断言**客户端实际收到的**重复响应头。

    这是 E1-26 修复的验收点：helper 返回了正确的列表，但两个调用点包了
    ``dict()``，于是两个 ``Set-Cookie`` 到客户端只剩一个。断言必须落在这一层 ——
    断言中间产物的话，那个 bug 一直是绿的。
    """

    def test_client_receives_both_set_cookies(self, settings: Settings) -> None:
        from tests.test_api_relay import _app as build

        up = FakeUpstream(
            handlers={
                "/v1/chat/completions": httpx.Response(
                    200,
                    content=b'data: {"choices":[],"usage":null}\n\ndata: [DONE]\n\n',
                    headers=[
                        ("content-type", "text/event-stream"),
                        ("set-cookie", "s1=1"),
                        ("set-cookie", "s2=2"),
                        ("www-authenticate", 'Bearer realm="a"'),
                        ("www-authenticate", 'Basic realm="b"'),
                    ],
                )
            }
        )
        with TestClient(build(settings, up)) as c, c.stream(
            "POST", CHAT, json={"model": "space-bunny-free", "stream": True}
        ) as r:
            r.read()
            assert r.headers.get_list("set-cookie") == ["s1=1", "s2=2"]
            assert r.headers.get_list("www-authenticate") == [
                'Bearer realm="a"',
                'Basic realm="b"',
            ]


class TestStreaming:
    def test_stream_is_delivered_intact(self, client: TestClient) -> None:
        with client.stream("POST", CHAT, json={"model": "space-bunny-free", "stream": True}) as r:
            payload = b"".join(r.iter_bytes())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert payload.startswith(b"data: ")
        assert payload.rstrip().endswith(b"data: [DONE]")

    def test_stream_headers_disable_buffering(self, client: TestClient) -> None:
        with client.stream("POST", CHAT, json={"model": "space-bunny-free", "stream": True}) as r:
            assert r.headers["x-accel-buffering"] == "no"
            assert r.headers["cache-control"] == "no-cache"
            r.read()

    def test_usage_is_injected_upstream(self, client: TestClient, upstream: FakeUpstream) -> None:
        with client.stream("POST", CHAT, json={"model": "space-bunny-free", "stream": True}) as r:
            r.read()
        assert json.loads(upstream.last().content)["stream_options"] == {"include_usage": True}

    def test_stream_without_usage_frame_is_recorded_unknown(
        self, settings: Settings
    ) -> None:
        up = FakeUpstream(handlers={CHAT: sse_response(sse_chunks(usage=NO_USAGE))})
        with TestClient(_app(settings, up)) as c:
            with c.stream("POST", CHAT, json={"model": "space-bunny-free", "stream": True}) as r:
                r.read()
            c.post("/api/admin/maintenance/flush")
            item = c.get("/api/admin/usage").json()["items"][0]
        assert item["usage_known"] is False
        assert item["total_tokens"] == 0
        assert item["stream"] is True

    def test_stream_usage_is_recorded(self, client: TestClient) -> None:
        with client.stream("POST", CHAT, json={"model": "space-bunny-free", "stream": True}) as r:
            r.read()
        client.post("/api/admin/maintenance/flush")
        item = client.get("/api/admin/usage").json()["items"][0]
        assert item["usage_known"] is True
        assert item["total_tokens"] == 167
        assert item["cached_tokens"] == 157


class TestRecordings:
    def test_usage_appears_in_the_admin_listing(self, client: TestClient) -> None:
        chat(client)
        chat(client, model="ling-3.1-flash-free")
        client.post("/api/admin/maintenance/flush")
        listing = client.get("/api/admin/usage").json()
        assert listing["total"] == 2
        assert set(listing["models"]) == {"space-bunny-free", "ling-3.1-flash-free"}

    def test_rejected_calls_are_recorded_too(self, client: TestClient) -> None:
        """被本站拒绝的调用也要进报表，否则「为什么我的请求没被转发」永远查不出来。"""
        client.post(CHAT, json={"model": "gpt-5.6-sol", "messages": []})
        client.post("/api/admin/maintenance/flush")
        item = client.get("/api/admin/usage").json()["items"][0]
        assert item["ok"] is False
        assert item["status"] == 400
        assert item["error_kind"] == "model_not_allowed"

    def test_client_ip_is_recorded_from_the_socket(self, client: TestClient) -> None:
        chat(client)
        client.post("/api/admin/maintenance/flush")
        item = client.get("/api/admin/usage").json()["items"][0]
        assert item["client_ip"] == "testclient"

    def test_forwarded_for_header_is_ignored(self, client: TestClient) -> None:
        """``X-Forwarded-For`` 是客户端自报的。本站默认绑 127.0.0.1，把它当真会让
        「按 IP 统计」变成一个谁都能随手伪造的字段。"""
        client.post(
            CHAT,
            json={"model": "space-bunny-free", "messages": []},
            headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8", "X-Real-IP": "9.9.9.9"},
        )
        client.post("/api/admin/maintenance/flush")
        item = client.get("/api/admin/usage").json()["items"][0]
        assert item["client_ip"] == "testclient"

    def test_prompts_are_never_persisted(self, client: TestClient) -> None:
        secret = "这是不应该出现在任何存储里的提示词"
        client.post(CHAT, json={"model": "space-bunny-free",
                                "messages": [{"role": "user", "content": secret}]})
        client.post("/api/admin/maintenance/flush")
        body = client.get("/api/admin/usage?page_size=5").text
        assert secret not in body

    def test_counts_only_never_content(self, client: TestClient) -> None:
        chat(client)
        client.post("/api/admin/maintenance/flush")
        item = client.get("/api/admin/usage").json()["items"][0]
        assert set(item) >= {"prompt_tokens", "completion_tokens", "total_tokens", "latency_ms"}
        assert "messages" not in item
        assert "content" not in item


class TestAuthOverHttp:
    def test_default_is_open(self, client: TestClient) -> None:
        assert client.get("/v1/__health").json()["require_key"] is False
        assert chat(client).status_code == 200

    def test_switching_require_key_blocks_anonymous(self, client: TestClient) -> None:
        client.patch("/api/admin/settings", json={"require_key": True})
        response = chat(client)
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "missing_api_key"

    def test_switching_back_reopens(self, client: TestClient) -> None:
        client.patch("/api/admin/settings", json={"require_key": True})
        assert chat(client).status_code == 401
        client.patch("/api/admin/settings", json={"require_key": False})
        assert chat(client).status_code == 200

    def test_issued_key_is_accepted(self, client: TestClient) -> None:
        secret = client.post("/api/admin/keys", json={"name": "甲"}).json()["secret"]
        client.patch("/api/admin/settings", json={"require_key": True})
        response = client.post(CHAT, json={"model": "space-bunny-free", "messages": []},
                               headers={"Authorization": f"Bearer {secret}"})
        assert response.status_code == 200

    def test_attribution_follows_the_key(self, client: TestClient) -> None:
        secret = client.post("/api/admin/keys", json={"name": "甲"}).json()["secret"]
        client.post(CHAT, json={"model": "space-bunny-free", "messages": []},
                    headers={"Authorization": f"Bearer {secret}"})
        client.post("/api/admin/maintenance/flush")
        item = client.get("/api/admin/usage").json()["items"][0]
        assert item["anonymous"] is False
        assert item["key_label"] == "甲"


class TestUpstreamUnavailable:
    def test_refused_upstream_is_502(self, settings: Settings) -> None:
        with TestClient(_app(settings, FakeUpstream(handlers={"*": refuse}))) as c:
            response = c.post(CHAT, json={"model": "space-bunny-free", "messages": []})
        assert response.status_code == 502
        assert response.json()["error"]["type"] == "upstream_unreachable"


def _app(settings: Settings, transport: httpx.AsyncBaseTransport, **overrides: object) -> FastAPI:
    """按给定 transport 建一个 app。默认关掉 pruner 后台任务（测试不需要）。"""
    from openproxy.app import create_app

    return create_app(
        dataclasses.replace(settings, **overrides),  # type: ignore[arg-type]
        transport=transport,
        start_pruner=False,
        start_prober=False,
        tz_offset_minutes=480,
    )


@pytest.fixture
def upstream() -> FakeUpstream:
    return standard_upstream()
