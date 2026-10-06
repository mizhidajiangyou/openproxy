"""真实上游冒烟。

**为什么必须单独存在**：整个测试套件用的是假上游，它证明了转发链路的分帧、
用量抽取、错误透传、记账调用是对的 —— 但它证明不了**真实上游还能用**。
Cloudflare 规则、上游改协议、免费模型下线，这些只有真跑一次才知道。

默认不跑（``-m "not network"`` 可以排除整个套件时更干净）：

    uv run pytest -m network

它只做**只读或幂等**的调用：列模型、非流式一句话、流式一句话。不写库、
不改配置、不消耗任何需要付费的资源。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openproxy.config import DEFAULT_UPSTREAM_BASE, Settings
from openproxy.container import Container
from openproxy.domain import FREE_MODEL_IDS, ErrorKind, UsageFilter
from openproxy.service.model_catalog import ProbeResult
from openproxy.service.usage_extract import usage_from_json_body
from tests.support.asgi import collect, json_request, make_request

pytestmark = pytest.mark.network

CHAT = "/v1/chat/completions"
TIMEOUT = 120.0


def live_settings(tmp_path: Path) -> Settings:
    """刻意**不用** conftest 的 settings：那个把 db 指向 tmp_path 且用的是
    pytest 里固定的上游配置。这里要真的打到 opencode.ai。"""
    return Settings(
        db_path=str(tmp_path / "live.db"),
        upstream_base=DEFAULT_UPSTREAM_BASE,
        connect_timeout=20.0,
        read_timeout=TIMEOUT,
    )


@pytest.fixture
async def live(tmp_path):
    """真实容器。用 ``async def`` fixture 是为了能在 teardown 里 await
    ``http.aclose()`` —— 不关连接池的话 ResourceWarning 会在
    ``filterwarnings = ["error"]`` 下把测试打挂。"""
    container = Container.build(live_settings(tmp_path), tz_offset_minutes=480)
    try:
        yield container
    finally:
        await container.shutdown()


async def test_every_free_model_in_the_catalog_is_still_offered_upstream(live: Container) -> None:
    """静态清单里的模型如果上游已经下线，界面还在吹牛。这是清单唯一的漂移来源。"""
    result = await probe(live)
    assert result.ok, f"上游不可达: {result.detail}"
    missing = FREE_MODEL_IDS - result.available_ids
    assert not missing, f"清单里的模型上游已不再提供: {sorted(missing)}"


async def test_non_streaming_call_returns_real_usage(live: Container) -> None:
    model = "space-bunny-free"
    response = await live.proxy.handle(
        json_request({
            "model": model,
            "messages": [{"role": "user", "content": "回复一个词：好"}],
            "max_tokens": 16,
        })
    )
    assert response.status_code == 200, bytes(response.body)[:400]

    usage = usage_from_json_body(bytes(response.body))
    assert usage.known, "真实上游必须报 usage，否则统计无从谈起"
    assert usage.prompt_tokens > 0
    assert usage.total_tokens >= usage.prompt_tokens

    assert live.recorder.flush(5.0)
    record = live.usage.list(UsageFilter()).items[0]
    assert record.model == model
    assert record.status == 200
    assert record.usage.known is True
    assert record.usage.total_tokens > 0
    assert record.error_kind is ErrorKind.NONE


async def test_streaming_call_reports_usage_after_injection(live: Container) -> None:
    """本站注入 ``stream_options.include_usage`` 之后，流式也必须拿到真实用量 ——
    这是「流式完全统计不到」那个缺陷的端到端反证。"""
    response = await live.proxy.handle(
        json_request({
            "model": "space-bunny-free",
            "messages": [{"role": "user", "content": "从一数到五"}],
            "max_tokens": 64,
            "stream": True,
        })
    )
    assert response.status_code == 200
    payload = await collect(response)
    assert payload.startswith(b"data: ")
    assert b"[DONE]" in payload

    assert live.recorder.flush(5.0)
    record = live.usage.list(UsageFilter()).items[0]
    assert record.stream is True
    assert record.usage.known is True, "注入 include_usage 后仍拿不到用量"
    assert record.usage.total_tokens > 0


async def test_outgoing_user_agent_survives_the_real_cloudflare(live: Container) -> None:
    """回归：Cloudflare 以 ``403 error code: 1010`` 拒绝 ``Python-urllib/*``
    与缺失 UA。

    客户端头**必须**显式带上那个会被拦的 UA：不带的话 httpx 会自己填一个
    ``python-httpx/…``，那条也能过，于是这个用例证明不了「本站覆写了 UA」，
    只是证明「本站的默认 UA 没被拦」。
    """
    response = await live.proxy.handle(
        json_request(
            {
                "model": "space-bunny-free",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8,
            },
            headers={"User-Agent": "Python-urllib/3.14"},
        )
    )
    body = bytes(response.body)
    assert response.status_code == 200, f"真实上游拒绝了本站的出站请求: {body[:300]!r}"


async def test_bogus_client_credential_never_reaches_upstream(live: Container) -> None:
    """客户端的占位密钥若被转发，上游会回 401。跑一次真的，确认它被吞掉。"""
    response = await live.proxy.handle(
        json_request(
            {
                "model": "space-bunny-free",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8,
            },
            headers={"Authorization": "Bearer sk-definitely-not-a-real-key"},
        )
    )
    assert response.status_code == 200, bytes(response.body)[:300]


async def test_unknown_model_is_rejected_locally(live: Container) -> None:
    """本站应先拦成 400，而不是转上去换一个含义错误的 401 ModelError。"""
    response = await live.proxy.handle(json_request({"model": "no-such-model", "messages": []}))
    assert response.status_code == 400
    assert json.loads(bytes(response.body))["error"]["type"] == "model_not_allowed"


async def test_get_models_passes_through(live: Container) -> None:
    response = await live.proxy.handle(make_request("GET", "/v1/models"))
    assert response.status_code == 200
    data = json.loads(bytes(response.body))
    assert len(data["data"]) > 10


async def probe(container: Container) -> ProbeResult:
    """跑一次真实的上游探测。"""
    return await container.catalog.probe(
        container.http, container.config.snapshot.upstream_base
    )
