"""第一轮 review 的 P0 修复各自的回归测试。

每个测试都对着一条具体的 review 结论，docstring 里写清「如果不修会怎样」——
这样将来有人想改回去时，会先看到后果而不是只看到一行断言。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from openproxy.config import Overlays, Settings
from openproxy.domain import ErrorKind
from openproxy.service.opencode_client import (
    OpencodeError,
    OpencodeSettings,
    complete,
)

BODY = {"model": "big-pickle", "messages": [{"role": "user", "content": "7*8?"}]}


# ------------------------------------------------------ P0-4 validated() ---


class TestValidatedIsCumulative:
    """P0-4：``validated()`` 曾是「每个字段各自 early return」，
    于是**后面的字段永远洗不到**。

    症状：同时动 ``upstream_base`` 与 ``opencode_models`` 时，
    模型名的 strip 被跳过 → 存下带空格的模型名 →
    「开关显示已勾选但请求永远不走 opencode」；而重启后
    ``decode_overlays`` 反而会 strip 自愈 —— 「重启前不生效、重启后生效」。
    """

    def test_opencode_models_cleaned_even_when_base_is_dirty(self) -> None:
        v = Overlays(
            upstream_base="http://example.test/zen  ",
            opencode_models=(" big-pickle ", "", "  ", "a", "a"),
        ).validated()
        assert v.opencode_models == ("big-pickle", "a")
        assert v.upstream_base == "http://example.test/zen"

    def test_opencode_models_cleaned_when_effort_is_blank(self) -> None:
        """``reasoning_effort`` 的空串现在保留为 ``""``（显式关闭），
        不再归一成 ``None`` —— 但**无论它变成什么，都不能阻止
        ``opencode_models`` 的清洗**。这才是这条测试要守的东西。"""
        v = Overlays(reasoning_effort="  ", opencode_models=(" x ",)).validated()
        assert v.reasoning_effort == "", "空串= 显式关闭，不该被归成 None"
        assert v.opencode_models == ("x",), "opencode_models 的清洗被跳过了"

    def test_all_fields_cleaned_at_once(self) -> None:
        """四个字段同时需要规范化时，每一个都要被处理。"""
        v = Overlays(
            upstream_base="  http://a.test/zen  ",
            reasoning_effort="  high  ",
            opencode_models=(" m ",),
        ).validated()
        assert v.upstream_base == "http://a.test/zen"
        assert v.reasoning_effort == "high"
        assert v.opencode_models == ("m",)

    def test_nothing_dirty_returns_self(self) -> None:
        """没有字段需要规范化时返回原对象（不 gratuitous 地 replace）。"""
        v = Overlays(opencode_models=("a", "b"))
        assert v.validated() is v


# ------------------------------------------- P0-3 400 的 error_kind 归类 ---


class TestBadRequestKind:
    """P0-3：协议不支持的 400 曾被记成 ``CLIENT_DISCONNECT``。

    症状：渠道页「失败原因拆解」把这类请求归进「客户端断开」桶，
    且与 ``rejection_kind()`` 里已有的 ``bad_request → BAD_REQUEST`` 自相矛盾 ——
    同一种 400 在两条路径上被记成两个类别。
    """

    def test_non_chat_protocol_is_bad_request(self) -> None:
        from openproxy.service.proxy import _prompt_of

        # 只守住前提：形状不对时 prompt 提取必然失败-> 调用方回 400。
        assert _prompt_of(json.dumps({"input": "hi"}).encode()) == ""

    def test_error_kind_is_bad_request_end_to_end(self) -> None:
        """**这条曾零覆盖** —— 第二轮 review 做变异测试时发现：把
        ``BAD_REQUEST`` 改回 ``CLIENT_DISCONNECT``，763 项测试**仍然全绿**。
        第一轮修对了但没被钉住。

        所以这里断言的是**端到端之后ctx.error_kind 的值**，
        而不是「函数返回了空串」这种间接推断 —— 只有这样才能杀掉那个变异。
        """
        from openproxy.container import Container
        from openproxy.service.proxy import ProxyService

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=str(Path(tmp) / "t.db"),
                opencode_base="http://127.0.0.1:9",
                opencode_password="pw",
            )
            container = Container.build(settings, tz_offset_minutes=480)
            try:
                ctx = _make_ctx()
                resp = asyncio.run(ProxyService._forward_via_opencode(
                    container.proxy,
                    ctx,
                    json.dumps({"model": "big-pickle", "input": "hi"}).encode(),
                ))
                assert resp.status_code == 400
                assert ctx.error_kind is ErrorKind.BAD_REQUEST, (
                    f"error_kind 应为 bad_request，实际 {ctx.error_kind}"
                )
            finally:
                container.recorder.close()
                container.database.close()


# --------------------------------------- P0-2 bytes_out 必须在 _finish 前 ---


class TestBytesOutOrdering:
    """P0-2：``_finish(ctx)`` 曾排在 SSE 分支**之前**，
    于是 ``ctx.bytes_out = len(sse)`` 是死代码（落库已完成）。

    症状：流式调用记的是非流式 JSON 的长度（实测 285 vs 实际 673，少算 60%+），
    而报表上完全看不出来。这个测试直接比「落库的bytes_out」与「实际响应体长度」。
    """

    def test_stream_bytes_out_matches_the_bytes_we_send(self) -> None:
        import hashlib
        import tempfile

        from fastapi.testclient import TestClient

        from openproxy.app import create_app
        from openproxy.domain import UsageFilter
        from openproxy.service.config_service import OVERLAY_KEY, encode_overlays
        from openproxy.store import Database, UsageStore

        class Fake:
            SID = "ses_p0"

            def handle(self, request: httpx.Request) -> httpx.Response:
                p, m = request.url.path, request.method
                if p == "/api/session" and m == "POST":
                    return httpx.Response(200, json={"data": {"id": self.SID}})
                if p == f"/api/session/{self.SID}/prompt":
                    return httpx.Response(200, json={"delivery": "steer"})
                if p == f"/api/session/{self.SID}/message":
                    return httpx.Response(200, json={"data": [
                        {"type": "assistant", "model": {"id": "m"},
                         "content": [{"type": "text", "text": "42"}],
                         "tokens": {"input": 3, "output": 4}},
                        {"type": "idle"},
                    ]})
                if p == f"/api/session/{self.SID}":
                    return httpx.Response(200, json={})
                if p.startswith("/v1/"):
                    return httpx.Response(200, json={
                        "id": "x", "object": "chat.completion", "created": 0,
                        "model": "m", "choices": [{"index": 0,
                            "message": {"content": "直连"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                    )
                return httpx.Response(404, json={"_tag": "NotFound"})

        fake = Fake()

        async def dispatch(request: httpx.Request) -> httpx.Response:
            if request.url.host in ("127.0.0.1", "localhost"):
                return fake.handle(request)
            return httpx.Response(200, json={"data": [], "object": "list"})

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "t.db")
            settings = Settings(
                db_path=db_path,
                opencode_base="http://127.0.0.1:9",
                opencode_password="pw",
            )
            d = Database(Path(db_path))
            d.migrate()
            d.kv_set(OVERLAY_KEY, encode_overlays(
                Overlays(opencode_models=("big-pickle",))
            ))
            d.close()

            app = create_app(settings, transport=httpx.MockTransport(dispatch),
                             start_pruner=False, start_prober=False,
                             tz_offset_minutes=480)
            client = TestClient(app)
            r = client.post("/v1/chat/completions", json={**BODY, "stream": True})
            assert r.status_code == 200, r.text
            actual = len(r.content)
            client.post("/api/admin/maintenance/flush")

            d2 = Database(Path(db_path))
            page = UsageStore(d2, tz_offset_minutes=480).list(UsageFilter(page_size=5))
            d2.close()
            assert page.total == 1, "调用没落库"
            assert page.items[0].bytes_out == actual, (
                f"落库 bytes_out={page.items[0].bytes_out}，实际发送={actual}"
            )
            del hashlib


# --------------------------------------- P0-1 客户端断开仍必须记账 ---------


class TestClientDisconnectIsRecorded:
    """P0-1：``await complete(...)`` 期间客户端断开 → task 被取消 →
    ``CancelledError``（``BaseException`` 的子类）既不被
    ``except OpencodeError`` 也不被 ``handle()`` 的 ``except Exception`` 捕获。

    而 opencode 侧的 prompt **已经投递、模型已经在生成**，额度已经花掉了 ——
    不记账的话这一次调用会在报表里彻底消失。

    直通路径靠 ``_TeeStream`` 解决了同一问题；这里等的是 future，那套用不上。
    """

    @pytest.mark.asyncio
    async def test_cancel_still_records(self) -> None:
        """直接测 ``_forward_via_opencode`` 的取消路径。

        用**真实的容器**（``Container.build``）而不是 ``ProxyService.__new__`` ——
        后者要手工拼 ``_config_provider`` / ``_finish`` / ``_http`` 三个内部字段，
        拼错一个就是 ``AttributeError``，而那种测试在实现重构后会集体失效。
        """
        import tempfile

        from openproxy.container import Container
        from openproxy.service.proxy import ProxyService

        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=str(Path(tmp) / "t.db"),
                opencode_base="http://127.0.0.1:9",
                opencode_password="pw",
            )
            container = Container.build(settings, tz_offset_minutes=480)
            try:
                # 替换代理层的 httpx 客户端，让第一个请求（建会话）直接被取消。
                # 刻意用真实的 ``httpx.AsyncClient`` + ``MockTransport`` 而不是
                # 自己编一个 client 类 —— ``_forward_via_opencode`` 只调
                # ``.request()``，但用真类型能让 mypy 继续检查我们没破坏别的约定，
                # 而手写一个类就得靠 type: ignore 掩盖「它满不满足 httpx 的协议」。
                def _always_cancel(request: httpx.Request) -> httpx.Response:
                    raise asyncio.CancelledError()

                container.proxy._http = httpx.AsyncClient(
                    transport=httpx.MockTransport(_always_cancel)
                )

                ctx = _make_ctx()
                with pytest.raises(asyncio.CancelledError):
                    await ProxyService._forward_via_opencode(
                        container.proxy,
                        ctx,
                        json.dumps(BODY).encode(),
                    )
                assert ctx.status == 499, (
                    f"状态应是 499（客户端断开），实际 {ctx.status}"
                )
                assert ctx.error_kind is ErrorKind.CLIENT_DISCONNECT
                assert ctx.recorded, "取消时必须先记账再重抛"
            finally:
                container.recorder.close()
                container.database.close()


def _make_ctx() -> Any:
    """构造一个够用的 ``CallContext``。

    **用真实的 dataclass 构造器**而不是 ``type("Ctx", (), {})()`` + 手工塞字段 ——
    后者漏一个字段就是运行到一半才``AttributeError``，而且实现加字段后不会失败
    （测试照样「通过」），直到某天那个字段真被用到才炸。
    """
    import time

    from openproxy.domain.models import TokenUsage
    from openproxy.service.auth import KeyState, ResolvedClient
    from openproxy.service.proxy import CallContext
    from openproxy.service.usage_extract import parse_request_meta

    return CallContext(
        started=time.monotonic(),
        path="/v1/chat/completions",
        client=ResolvedClient(state=KeyState.ANONYMOUS),
        meta=parse_request_meta(json.dumps(BODY).encode()),
        usage=TokenUsage.unknown(),
    )


# ----------------------------------------- P1-4 error/aborted 立刻退出 -----


class TestTerminalTypes:
    """P1-4：``_TERMINAL_TYPES`` 定义了却从未使用 —— 只有 ``idle`` 被处理。
    症状：上游一次 5xx，白等 poll_timeout（实测 120 秒 ≈ 340 次轮询 HTTP）。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["error", "aborted"])
    async def test_error_or_aborted_exits_immediately(self, kind: str) -> None:
        polls = {"n": 0}

        class Client:
            async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
                path = url.split("/", 3)[-1]
                if path == "api/session":
                    return httpx.Response(200, json={"data": {"id": "s"}})
                if path.endswith("/prompt"):
                    return httpx.Response(200, json={})
                if path.endswith("/message"):
                    polls["n"] += 1
                    return httpx.Response(200, json={"data": [
                        {"type": kind, "error": {"message": "上游 5xx"}},
                    ]})
                return httpx.Response(200, json={})

        settings = OpencodeSettings(
            password="pw", poll_interval=0.0, poll_timeout=30.0
        )
        with pytest.raises(OpencodeError) as exc:
            await complete(Client(), settings, "q", model="m")
        assert polls["n"] == 1, f"轮询了 {polls['n']} 次，应该 1 次就退出"
        assert "上游 5xx" in str(exc.value), "异常里要带出真实原因"

    @pytest.mark.asyncio
    async def test_idle_with_text_still_works(self) -> None:
        """回归：正常路径不受影响。"""

        class Client:
            async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
                path = url.split("/", 3)[-1]
                if path == "api/session":
                    return httpx.Response(200, json={"data": {"id": "s"}})
                if path.endswith("/prompt"):
                    return httpx.Response(200, json={})
                if path.endswith("/message"):
                    return httpx.Response(200, json={"data": [
                        {"type": "assistant", "model": {"id": "m"},
                         "content": [{"type": "text", "text": "ok"}]},
                        {"type": "idle"},
                    ]})
                return httpx.Response(200, json={})

        reply = await complete(
            Client(), OpencodeSettings(password="pw", poll_interval=0.0),
            "q", model="m",
        )
        assert reply.text == "ok"


class TestUsagePayloadShape:
    """B-4：``_usage_payload``曾丢掉 ``cached_tokens`` / ``reasoning_tokens``。

    症状：落库的 ``TokenUsage`` 带这两个字段，但回给客户端的 ``usage`` 只有
    三个 —— 客户端算不出非缓存 token 数，也看不到推理开销。
    """

    def test_details_are_passed_through(self) -> None:
        from openproxy.service.opencode_client import OpencodeReply
        from openproxy.service.proxy import _usage_payload

        payload = _usage_payload(OpencodeReply(
            text="x", model="m",
            usage={"prompt_tokens": 100, "completion_tokens": 50,
                   "cached_tokens": 30, "reasoning_tokens": 20},
        ))
        assert payload["prompt_tokens"] == 100
        assert payload["completion_tokens"] == 50
        assert payload["total_tokens"] == 150
        # OpenAI 的嵌套形状，与直通路径 usage_extract._NESTED_FIELDS 一致
        assert payload["prompt_tokens_details"] == {"cached_tokens": 30}
        assert payload["completion_tokens_details"] == {"reasoning_tokens": 20}

    def test_details_are_omitted_when_absent(self) -> None:
        """没有就**不放这个键** —— 放了空值会让客户端以为「统计了但是 0」。"""
        from openproxy.service.opencode_client import OpencodeReply
        from openproxy.service.proxy import _usage_payload

        payload = _usage_payload(OpencodeReply(text="x", model="m"))
        assert payload == {"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0}
        assert "prompt_tokens_details" not in payload
        assert "completion_tokens_details" not in payload


class TestReasoningIsSurfaced:
    """B-7：``reply.reasoning`` 曾被采集但**全库零消费**。

    症状：用户看到「有思考 token 消耗、但思考内容查不到」，而强制思考档位的
    效果**在界面上完全无法验证** —— 而那是这个功能唯一的验证途径。
    """

    def test_non_stream_payload_carries_reasoning_content(self) -> None:
        from openproxy.service.opencode_client import OpencodeReply
        from openproxy.service.proxy import _forward_via_opencode_payload

        reply = OpencodeReply(text="答案", model="m", reasoning="先算 7*8")
        payload = _forward_via_opencode_payload(reply, "big-pickle", key_id=None)
        msg = payload["choices"][0]["message"]
        assert msg["content"] == "答案"
        assert msg["reasoning_content"] == "先算 7*8"

    def test_no_reasoning_means_no_key(self) -> None:
        """没有思考时**不放这个键**，而不是放空串 ——
        部分客户端见到空串会当成「思考了但内容被清空」。"""
        from openproxy.service.opencode_client import OpencodeReply
        from openproxy.service.proxy import _forward_via_opencode_payload

        payload = _forward_via_opencode_payload(
            OpencodeReply(text="答案", model="m"), "big-pickle", key_id=None
        )
        assert "reasoning_content" not in payload["choices"][0]["message"]

    def test_sse_has_a_reasoning_frame(self) -> None:
        from openproxy.service.opencode_client import OpencodeReply
        from openproxy.service.proxy import _as_sse, _forward_via_opencode_payload

        payload = _forward_via_opencode_payload(
            OpencodeReply(text="答案", model="m", reasoning="思考中"),
            "big-pickle", key_id=None,
        )
        frames = [
            f for f in _as_sse(payload).split(b"\n\n")
            if f.startswith(b"data: ") and f != b"data: [DONE]"
        ]
        # role → reasoning → content → finish(+usage)
        assert len(frames) == 4
        deltas = [
            json.loads(f[len(b"data: "):])["choices"][0]["delta"] for f in frames
        ]
        assert deltas[1].get("reasoning_content") == "思考中"
        assert deltas[2].get("content") == "答案"

    def test_sse_without_reasoning_has_three_frames(self) -> None:
        from openproxy.service.opencode_client import OpencodeReply
        from openproxy.service.proxy import _as_sse, _forward_via_opencode_payload

        payload = _forward_via_opencode_payload(
            OpencodeReply(text="答案", model="m"), "big-pickle", key_id=None
        )
        frames = [
            f for f in _as_sse(payload).split(b"\n\n")
            if f.startswith(b"data: ") and f != b"data: [DONE]"
        ]
        # 没有思考帧（而不是发一个空串帧）
        assert len(frames) == 3


class TestPollTimeoutBoundsInFlightRequest:
    """B-2：``poll_timeout`` 曾只管住 ``while`` 头部，管不住「正在飞的那一次
    GET」—— 而那正是「opencode 挂起但 TCP 不断」时的等待来源。

    实测：poll_timeout=0.3s 而单次 GET 卡 2s -> 实际 2.00s（6.7 倍预算）；
    生产默认值下最坏总时长 = poll_timeout(120s) + 单次 GET 卡满(600s) = 12 分钟。
    """

    @pytest.mark.asyncio
    async def test_single_request_is_capped_to_remaining_budget(self) -> None:
        """断言「单次请求用的 timeout <= 剩余预算」，而不是真的等 2 秒。"""
        seen: list[float | None] = []

        class Client:
            async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
                t = kw.get("timeout")
                seen.append(getattr(t, "read", None))
                path = url.split("/", 3)[-1]
                if path == "api/session":
                    return httpx.Response(200, json={"data": {"id": "s"}})
                if path.endswith("/prompt"):
                    return httpx.Response(200, json={})
                if path.endswith("/message"):
                    return httpx.Response(200, json={"data": [
                        {"type": "assistant", "model": {"id": "m"},
                         "content": [{"type": "text", "text": "ok"}]},
                        {"type": "idle"},
                    ]})
                return httpx.Response(200, json={})

        settings = OpencodeSettings(
            password="pw", read_timeout=600.0, poll_interval=0.0, poll_timeout=5.0
        )
        await complete(Client(), settings, "q", model="m")
        # 只有**轮询那次** GET 该被压到剩余预算内；建会话 / prompt / DELETE
        # 不受 poll_timeout 约束（它们是一次性的短请求，用 read_timeout 合理）。
        # 所以这里断言「至少有一次被压住」，而不是「所有都被压住」。
        assert seen, "没有任何请求被记录"
        assert any(t is not None and t <= 5.0 for t in seen), (
            f"没有任何请求被压到 poll_timeout(5s) 内，实际 {seen}"
        )
        assert min(t for t in seen if t is not None) <= 5.0


# ---------------------------------------------- P1-1 opencode_base 校验 ---


class TestOpencodeBaseValidation:
    """P1-1：``opencode_base`` 曾零校验（``upstream_base`` 有）。

    后果：``file:///etc/passwd`` 能过 → httpx 抛 ``UnsupportedProtocol``
    → 被 ``except Exception`` 归成「服务不可达」，报的错误与真正原因不符。
    """

    @pytest.mark.parametrize("bad", [
        "file:///etc/passwd", "gopher://x", "not-a-url", "",
        "ftp://h/x", "javascript:alert(1)",
    ])
    def test_rejects_non_http(self, bad: str) -> None:
        from openproxy.config import ConfigError

        with pytest.raises(ConfigError):
            Settings(opencode_base=bad)

    @pytest.mark.parametrize("good", [
        "http://127.0.0.1:4096", "https://oc.internal:8443",
    ])
    def test_accepts_http_and_https(self, good: str) -> None:
        assert Settings(opencode_base=good).opencode_base == good

    def test_remote_hosts_are_allowed(self) -> None:
        """刻意允许远程主机 —— 有人会把 opencode 跑在容器或另一台机器上。
        只校验协议，不校验主机名（校验主机名会把那类部署直接挡掉）。"""
        assert Settings(opencode_base="http://10.0.0.5:4096").opencode_base

    def test_timeout_range_is_checked(self) -> None:
        from openproxy.config import ConfigError

        with pytest.raises(ConfigError):
            Settings(opencode_timeout=0.0)
        with pytest.raises(ConfigError):
            Settings(opencode_timeout=99999.0)
