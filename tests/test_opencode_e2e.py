"""端到端验证「哪些模型走哪条路」。

这是整套分流里**最容易出静默错误**的一环：某个模型本该直通却被转走时，
症状只是「它突然需要 opencode 在跑」，而控制台上一片正常（因为 opencode 侧
统计一切正常）。这类 bug 只有端到端才看得见。

**不注入任何测试专用钩子**：配置走真实的「写库 → ConfigService 读库 → 合成」，
所以测的就是线上跑的那条路径。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openproxy.app import create_app
from openproxy.config import Overlays, Settings
from openproxy.domain import UsageFilter
from openproxy.service.config_service import OVERLAY_KEY, encode_overlays
from openproxy.store import Database, UsageStore

FIXED_TZ = 480

#: 夹具产出「给模型集合→ 构造 app」的函数 + 那个假 opencode 服务。
#: 抽成别名是因为有 10 个测试方法的参数要用它 —— 每次重写一遍
#: ``tuple[Callable[[tuple[str, ...]], TestClient], FakeOpencode]``
#: 只会让签名变长、不会变清楚。
Routed = tuple[Callable[[tuple[str, ...]], TestClient], "FakeOpencode"]


class FakeOpencode:
    """假 opencode 服务。会话 id固定，便于断言回收。"""

    SID = "ses_fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.deleted: list[str] = []
        self.fail_with: int | None = None

    def transport(self) -> object:
        return _Router(self)


class _Router:
    """把 FakeOpencode 的回放逻辑（与 httpx 无关的部分）。

    刻意**显式标注类型**而不是让 ``handle`` 返回 Any：一旦这里退化成 Any，
    ``dispatch`` 也就返回 Any，而 ``create_app`` 收的是
    ``httpx.AsyncBaseTransport`` —— 类型链一断，mypy 就再也查不出
    「这个 transport 到底满不满足协议」了。
    """

    def __init__(self, fake: FakeOpencode) -> None:
        self.fake = fake

    def handle(self, request: httpx.Request) -> httpx.Response:
        f = self.fake
        path, method = request.url.path, request.method
        if f.fail_with and path == "/api/session":
            return _json(f.fail_with, {"_tag": "UnauthorizedError"})
        if path == "/api/session" and method == "POST":
            return _json(200, {"data": {"id": f.SID}})
        if path == f"/api/session/{f.SID}/prompt" and method == "POST":
            f.prompts.append(json.loads(request.content)["text"])
            return _json(200, {"delivery": "steer"})
        if path == f"/api/session/{f.SID}/message":
            return _json(200, {"data": [
                {"type": "assistant", "model": {"id": "fledge-alpha-free"},
                 "content": [{"type": "text", "text": "42"},
                             {"type": "reasoning", "text": "算一下"}],
                 "tokens": {"input": 11, "output": 22, "reasoning": 5}},
                {"type": "idle", "outcome": "succeeded"},
            ]})
        if path == f"/api/session/{f.SID}" and method == "DELETE":
            f.deleted.append(f.SID)
            return _json(200, {})
        return _json(404, {"_tag": "NotFound"})


def _json(status: int, payload: object) -> httpx.Response:
    return httpx.Response(status_code=status, json=payload)


def _seed_overlay(settings: Settings, models: tuple[str, ...]) -> None:
    """把覆盖层写进库—— 走的是真实持久化路径。"""
    db = Database(Path(settings.db_path))
    db.migrate()
    db.kv_set(OVERLAY_KEY, encode_overlays(Overlays(opencode_models=models)))
    db.close()


def _make_client(
    settings: Settings,
    upstream: httpx.AsyncBaseTransport,
    fake: FakeOpencode,
) -> TestClient:
    """起一个 app：上游走 FakeUpstream，opencode 后端走 FakeOpencode。

    **按 host 分流**而不是替换整个 ``_http``：代理层的那个 client 负责**两条路**
    （直通打上游、走 opencode 打本地服务），整体换掉会让直通也打到假服务上去
    —— 症状是「直通的模型突然 404」，而那与本测试要测的东西无关。
    """
    router = _Router(fake)
    # ``upstream`` 已按 ``httpx.AsyncBaseTransport`` 标注，所以直接用 ——
    # 写成 ``object`` 就得靠 ``type: ignore`` 压制「它到底有没有
    # handle_async_request」，而那条ignore 掩盖的正是「这个 transport 满不满足
    # 协议」这个我们真正在意的检查。
    async def dispatch(request: httpx.Request) -> httpx.Response:
        # opencode 后端固定在 127.0.0.1（见 settings 里的 opencode_base），
        # 上游是 opencode.ai —— 用 host 区分，两条路各回各的。
        if request.url.host in ("127.0.0.1", "localhost"):
            return router.handle(request)
        return await upstream.handle_async_request(request)

    app: FastAPI = create_app(
        settings, transport=httpx.MockTransport(dispatch), start_pruner=False,
        start_prober=False, tz_offset_minutes=FIXED_TZ,
    )
    return TestClient(app)


@pytest.fixture
def routed(
    tmp_path: Path, upstream: httpx.AsyncBaseTransport
) -> Routed:
    """产出 ``(build, fake)``：给模型集合即可构造 app。"""
    fake = FakeOpencode()

    def build(models: tuple[str, ...]) -> TestClient:
        s = Settings(
            db_path=str(tmp_path / "openproxy.db"),
            opencode_base="http://127.0.0.1:9",
            opencode_password="pw",
        )
        _seed_overlay(s, models)
        return _make_client(s, upstream, fake)

    return build, fake


BODY = {"model": "big-pickle", "messages": [{"role": "user", "content": "7*8?"}]}


class TestNoToolSuffix:
    """转发给 opencode 的 prompt **必须**追加「不要用工具」的约束句。

    ## 这不是锦上添花（实测 2026-10-06）

    opencode 是 agent，每个新会话都会读全局 ``AGENTS.md``，而那份文件写着
    「必须先读取 xxx」-> 它就调``read`` 工具去读。而读文件**必须先要人批准**，
    HTTP 投递时没人批，那个工具永远停在``status: "running"``，
    于是**一直等到本站超时**，日志里只有一句「没有回复内容」。

    实测（同一服务、同一模型、同一 prompt，各 3 次）：

    =======================  ======  ======
    模型                不加后缀  加后缀
    =======================  ======  ======
    ``fledge-alpha-free``   2/3 卡住  0/3
    ``mimo-v2.6-flash-free``  3/3 卡住  0/3
    =======================  ======  ======
    """

    def test_suffix_covers_the_three_things(self) -> None:
        """三处措辞各防一件事，少一处就漏一个失败模式。"""
        from openproxy.service.proxy import OPENCODE_NO_TOOL_SUFFIX as S

        assert "不要使用任何工具" in S, (
            "只禁「读文件」不够 —— 模型仍可能调 glob/grep 去找那个文件"
        )
        assert "不要遵循任何项目指令文件" in S, (
            "AGENTS.md 是**系统级指令**，优先级高于用户 prompt 里的内容。"
            "不点破的话模型会认为用户这句话不如 AGENTS.md，于是照旧去读"
        )
        assert "直接回答" in S, (
            "禁了工具却不给替代动作，模型可能「不读但也不答」"
        )

    def test_empty_prompt_is_left_alone(self) -> None:
        """空 prompt 原样返回 —— 上游要报「没有可转的文本」那个 400。"""
        from openproxy.service.proxy import _with_no_tool_suffix

        assert _with_no_tool_suffix("") == ""
        assert _with_no_tool_suffix("   ") == "   "

    def test_normal_prompt_keeps_original_first(self) -> None:
        """原prompt 必须在**前面** —— 它是用户的话，不能被约束句替换掉。"""
        from openproxy.service.proxy import _with_no_tool_suffix

        got = _with_no_tool_suffix("7*8?")
        assert got.startswith("7*8?"), "用户的 prompt 丢了"

    @pytest.mark.asyncio
    async def test_the_prompt_reaching_opencode_has_the_suffix(
        self, routed: Routed
    ) -> None:
        """端到端：真正发出去的那句话里带约束句。

        这条比上面三条纯函数断言更重要 —— 纯函数只证明
        「拼上去了」，这条证明「**真的发出去了**」。
        """
        from openproxy.service.proxy import OPENCODE_NO_TOOL_SUFFIX

        make_client, fake = routed
        with make_client(("big-pickle",)) as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": "big-pickle",
                      "messages": [{"role": "user", "content": "7*8?"}]},
            )
        assert response.status_code == 200, response.text
        assert len(fake.prompts) == 1
        assert fake.prompts[0] == "7*8?" + OPENCODE_NO_TOOL_SUFFIX, (
            f"实际发出去的 prompt 不对：{fake.prompts[0]!r}"
        )


class TestRouting:
    def test_direct_by_default(self, routed: Routed) -> None:
        """默认全部直通 —— 假 opencode 一个请求都不该收到。"""
        build, fake = routed
        r = build(()).post("/v1/chat/completions", json={
            "model": "space-bunny-free",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 200, r.text
        assert fake.prompts == [], "默认不该走 opencode"
        # 直通时回显的就是上游回的模型（假上游只认 space-bunny-free）
        assert r.json()["model"] == "space-bunny-free"

    def test_selected_model_goes_via_opencode(self, routed: Routed) -> None:
        build, fake = routed
        r = build(("big-pickle",)).post("/v1/chat/completions", json=BODY)
        assert r.status_code == 200, r.text
        # prompt 末尾会追加「不要用工具」的约束句 —— 详见
        # openproxy.service.proxy.OPENCODE_NO_TOOL_SUFFIX。
        assert len(fake.prompts) == 1
        assert fake.prompts[0].startswith("7*8?")
        assert "不要使用任何工具" in fake.prompts[0]
        body = r.json()
        assert body["choices"][0]["message"]["content"] == "42"
        assert body["usage"]["total_tokens"] == 33  # 11 + 22
        # **实际走的**模型是 opencode 自己选的，如实回给客户端而不是回显请求值 ——
        # 回显请求值会掩盖「opencode 走了另一个模型」这个事实。
        assert body["model"] == "fledge-alpha-free"
        assert fake.deleted == [FakeOpencode.SID], "会话必须回收"

    def test_other_models_still_direct(self, routed: Routed) -> None:
        """**分流必须按模型**：没勾选的继续直通。"""
        build, fake = routed
        r = build(("big-pickle",)).post("/v1/chat/completions", json={
            "model": "fledge-alpha-free",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 200, r.text
        assert fake.prompts == [], "没勾选的模型不该被转走"

    def test_stream_request_gets_sse_shape(self, routed: Routed) -> None:
        """客户端要 stream:true 时必须回 SSE —— 形状变了客户端会直接报错。"""
        build, fake = routed
        r = build(("big-pickle",)).post("/v1/chat/completions", json={
            **BODY, "stream": True,
        })
        assert r.status_code == 200, r.text
        # prompt 末尾会追加「不要用工具」的约束句 —— 详见
        # openproxy.service.proxy.OPENCODE_NO_TOOL_SUFFIX。
        assert len(fake.prompts) == 1
        assert fake.prompts[0].startswith("7*8?")
        assert "不要使用任何工具" in fake.prompts[0]
        assert r.headers["content-type"].startswith("text/event-stream")
        assert b"data: [DONE]" in r.content
        assert b"42" in r.content

    def test_usage_lands_in_the_database(
        self, routed: Routed, tmp_path: Path
    ) -> None:
        """走 opencode 的调用**也要落库** —— 否则报表会漏掉这部分用量。"""
        build, _fake = routed
        client = build(("big-pickle",))
        client.post("/v1/chat/completions", json=BODY)
        client.post("/api/admin/maintenance/flush")

        # 库路径是**夹具定的**（``tmp_path / "openproxy.db"``），直接从 tmp_path 拼，
        # 而不是 ``client.app.state.container…`` —— TestClient.app 的类型是 ASGIApp，
        # 链上没有 ``state``，要拿就得 type: ignore，而那条 ignore 会连带掩盖
        # 「container.database 到底存不存在」这类真问题。
        db = Database(tmp_path / "openproxy.db")
        store = UsageStore(db, tz_offset_minutes=FIXED_TZ)
        page = store.list(UsageFilter(page_size=10))
        db.close()
        assert page.total >= 1, "走 opencode 的调用没有落库"
        row = page.items[0]
        # 落库的模型是**实际服务的那个**（opencode 自己选的），不是我们请求的
        assert row.model == "fledge-alpha-free"
        # token 数在 ``row.usage`` 里（UsageRecord 只存计数，不存正文）
        assert row.usage.completion_tokens == 22
        assert row.usage.prompt_tokens == 11
        assert row.usage.known is True

    def test_opencode_down_is_502_not_silent_fallback(self, routed: Routed) -> None:
        """opencode 挂了就明确报 502，**不自动回退直通**。

        自动回退听起来更稳，但会造成同一模型在两条路上静默切换 —— 客户端看到的
        价格、上下文、模型回答全都不一致，而日志里只有一行 warning。
        """
        build, fake = routed
        fake.fail_with = 401
        r = build(("big-pickle",)).post("/v1/chat/completions", json=BODY)
        assert r.status_code == 502, r.text
        assert "401" in r.text

    def test_non_chat_protocol_is_rejected_clearly(self, routed: Routed) -> None:
        """``/v1/responses`` 这类形状转不过去，要**明确 400**，
        而不是发一个含义错误的请求（那会让用户以为模型答错了）。"""
        build, fake = routed
        r = build(("big-pickle",)).post("/v1/responses", json={
            "model": "big-pickle", "input": "hi",
        })
        assert r.status_code == 400, r.text
        assert fake.prompts == []

    def test_get_models_never_rerouted(self, routed: Routed) -> None:
        """``GET /v1/models`` 没 body、也没有「模型」概念，不该被分流。"""
        build, fake = routed
        r = build(("big-pickle",)).get("/v1/models")
        assert r.status_code == 200, r.text
        assert fake.prompts == []

    def test_model_whitelist_still_applies(self, routed: Routed) -> None:
        """分流**不能绕过模型白名单**：勾了 opencode 的模型仍要是清单内的。"""
        build, fake = routed
        r = build(("not-in-catalog",)).post("/v1/chat/completions", json={
            "model": "not-in-catalog",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 400, r.text
        assert fake.prompts == []

    def test_reasoning_effort_does_not_break_the_opencode_path(self, routed: Routed) -> None:
        """开着强制思考时，opencode 路径**仍然要成功**。

        opencode 的 ``/prompt`` 端点只吃一个字符串，装不下 ``reasoning_effort`` ——
        所以本站的强制档位在这条路上**不生效**。这里断言的是「不要因此报错」：
        注入逻辑改的是 body 的顶层字段，而 opencode 路径只从 body 里取
        ``messages``，所以改写不会把请求弄坏（少一个功能好过把请求弄坏）。
        """
        build, fake = routed
        client = build(("big-pickle",))
        assert client.patch(
            "/api/admin/settings", json={"reasoning_effort": "high"}
        ).status_code == 200
        r = client.post("/v1/chat/completions", json=BODY)
        assert r.status_code == 200, r.text
        # prompt 末尾会追加「不要用工具」的约束句 —— 详见
        # openproxy.service.proxy.OPENCODE_NO_TOOL_SUFFIX。
        assert len(fake.prompts) == 1
        assert fake.prompts[0].startswith("7*8?")
        assert "不要使用任何工具" in fake.prompts[0]
