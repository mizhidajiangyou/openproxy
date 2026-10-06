"""「按模型分流到 opencode 服务」这套逻辑的测试。

分三类，因为它们坏的方式完全不同：

* **纯函数**（配置合成、清洗、文本提取）—— 错了会让开关状态与实际路由不一致，
  而这类不一致在界面上完全看不出来。
* **客户端协议**（与假opencode 服务对话）—— 错了会表现为「一直 502」，
  但根因可能是路径、参数形状、认证任一环。
* **端到端分流**（哪些模型走哪条路）—— 错了会让「本该直通的模型被转走」，
  症状是该模型突然需要 opencode 在跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

#: 模块本身。计数 ``discover_password`` 的调用次数时需要替换它的属性，
#: 而 ``from ... import discover_password`` 只拿到了一个绑定快照。
import openproxy.service.opencode_client as oc
from openproxy.config import Overlays, RuntimeConfig, load_settings
from openproxy.service.opencode_client import (
    OpencodeError,
    OpencodeSettings,
    complete,
    discover_password,
)
from openproxy.service.proxy import _as_sse, _prompt_of, _usage_payload

# --------------------------------------------------------------- 配置合成 ---


class TestCompose:
    def test_default_is_all_direct(self) -> None:
        """默认必须「全部直通」—— 没有 opencode 也能跑，行为与之前完全一致。"""
        config = RuntimeConfig.compose(load_settings({}))
        assert config.opencode_models == frozenset()
        assert config.uses_opencode("big-pickle") is False

    def test_env_var_baseline(self) -> None:
        settings = load_settings(
            {"OPENPROXY_MODELS_PLACEHOLDER": "", "OPENPROXY_OPENCODE_MODELS": "a, b ,a"}
        )
        config = RuntimeConfig.compose(settings)
        # 空串被丢掉、两边空白被 strip、重复被去掉但**保留首次出现的顺序**
        assert sorted(config.opencode_models) == ["a", "b"]
        assert config.uses_opencode("a") is True

    def test_overlay_wins_over_env(self) -> None:
        settings = load_settings({"OPENPROXY_OPENCODE_MODELS": "from-env"})
        config = RuntimeConfig.compose(settings, Overlays(opencode_models=("from-overlay",)))
        assert sorted(config.opencode_models) == ["from-overlay"]

    def test_empty_overlay_does_not_fall_back_to_env(self) -> None:
        """**这条最容易写错**：用户在控制台清空所有勾选后，
        环境变量里设的列表必须**不复活**。

        写成 ``eff.opencode_models or ()`` 就会把「显式清空」与「没设过」
        混成一件事，而症状是「我明明在界面上全取消了，它又自己回去了」。
        """
        settings = load_settings({"OPENPROXY_OPENCODE_MODELS": "from-env"})
        config = RuntimeConfig.compose(settings, Overlays(opencode_models=()))
        assert config.opencode_models == frozenset()

    def test_blank_and_duplicates_are_cleaned(self) -> None:
        cleaned = Overlays(
            opencode_models=("  a  ", "", "   ", "b", "a")
        ).validated()
        assert cleaned.opencode_models == ("a", "b")

    def test_uses_opencode_is_exact_match(self) -> None:
        """必须精确匹配，不能前缀/子串命中 ——
        ``big-pickle`` 命中了``big-pickle-2`` 会把请求转给一个不存在的模型。"""
        config = RuntimeConfig.compose(
            load_settings({}), Overlays(opencode_models=("big-pickle",))
        )
        assert config.uses_opencode("big-pickle") is True
        assert config.uses_opencode("big-pickle-2") is False
        assert config.uses_opencode("Big-Pickle") is False


# ----------------------------------------------------------- 文本与形状 ---


class TestPromptExtraction:
    def test_plain_messages(self) -> None:
        body = json.dumps({
            "model": "m",
            "messages": [
                {"role": "system", "content": "忽略"},
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "回答"},
                {"role": "user", "content": "第二问"},
            ],
        }).encode()
        # 只收 user：system 由 opencode 自己的 agent 决定，assistant 历史对它没意义
        assert _prompt_of(body) == "第一问\n\n第二问"

    def test_multimodal_keeps_text_parts(self) -> None:
        """多模态形状只取文字块 —— 图片转不过去，但静默丢掉整条更糟。"""
        body = json.dumps({
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                ],
            }],
        }).encode()
        assert _prompt_of(body) == "看这张图"

    @pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"messages":null}',
                                     b'{"messages":123}', b'{"messages":[{"role":"user"}]}'])
    def test_unusable_bodies_yield_empty(self, raw: bytes) -> None:
        """返回空串 = 调用方回 400，而不是发出一个含义错误的请求。"""
        assert _prompt_of(raw) == ""


class TestPayloadShape:
    def test_usage_payload_always_has_all_keys(self) -> None:
        """缺字段会被部分客户端读成「流式没开统计」而报错，所以缺项补 0。"""
        from openproxy.service.opencode_client import OpencodeReply

        payload = _usage_payload(OpencodeReply(text="x", model="m"))
        assert payload == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def test_sse_wrapping_has_all_frames(self) -> None:
        payload = {
            "id": "x", "created": 1, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        }
        frames = [f for f in _as_sse(payload).split(b"\n\n") if f.startswith(b"data: ")]
        # role → content → finish_reason(+usage) → [DONE]
        assert len(frames) == 4
        assert frames[-1] == b"data: [DONE]"
        last = json.loads(frames[2][len(b"data: "):])
        assert last["usage"]["total_tokens"] == 8
        assert last["choices"][0]["finish_reason"] == "stop"

    def test_sse_is_valid_json_per_frame(self) -> None:
        """每一帧都必须是独立合法 JSON —— 流式解析器逐帧 json.loads。"""
        payload = {"id": "x", "created": 1, "model": "m",
                   "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"content": "含引号\"与换行\n"}}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        for frame in _as_sse(payload).split(b"\n\n"):
            if frame.startswith(b"data: ") and frame != b"data: [DONE]":
                json.loads(frame[len(b"data: "):])  # 不抛即通过


# --------------------------------------------------------------- 客户端 ---


class FakeResponse:
    def __init__(self, status: int, payload: object, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload)
        self.headers: dict[str, str] = {}

    def json(self) -> object:
        return self._payload


class FakeClient:
    """按路径回放的假HTTP 客户端。记录每个请求，便于断言调用顺序。"""

    def __init__(self, script: dict[str, list[FakeResponse]]) -> None:
        self.script = script
        self.calls: list[tuple[str, str, object]] = []

    async def request(self, method: str, url: str, **kw: object) -> FakeResponse:
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path
        queue = self.script.get(path)
        if not queue:
            raise AssertionError(f"意料之外的请求：{method} {path}")
        self.calls.append((method, path, kw.get("json")))
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _settings(**kw: object) -> OpencodeSettings:
    base = {"base_url": "http://127.0.0.1:4096", "password": "pw",
            "poll_interval": 0.0, "poll_timeout": 5.0}
    return OpencodeSettings(**{**base, **kw})  # type: ignore[arg-type]


class TestClient:
    @pytest.mark.asyncio
    async def test_happy_path_deletes_session(self) -> None:
        client = FakeClient({
            "/api/session": [FakeResponse(200, {"data": {"id": "ses_1"}})],
            "/api/session/ses_1/prompt": [FakeResponse(200, {"delivery": "steer"})],
            "/api/session/ses_1/message": [FakeResponse(200, {"data": [
                {"type": "assistant", "model": {"id": "big-pickle"},
                 "content": [{"type": "text", "text": "56"}]},
                {"type": "idle", "outcome": "succeeded"},
            ]})],
            "/api/session/ses_1": [FakeResponse(200, {})],
        })
        reply = await complete(client, _settings(), "7*8?", model="big-pickle")
        assert reply.text == "56"
        assert reply.model == "big-pickle"
        # 会话必须被回收，否则 opencode 里会堆一堆空会话
        methods = [(m, p) for m, p, _ in client.calls]
        assert ("DELETE", "/api/session/ses_1") in methods

    @pytest.mark.asyncio
    async def test_reasoning_is_captured(self) -> None:
        """思考过程也要取 —— 只取正文的话强制思考的效果在控制台上看不见。"""
        client = FakeClient({
            "/api/session": [FakeResponse(200, {"data": {"id": "s"}})],
            "/api/session/s/prompt": [FakeResponse(200, {})],
            "/api/session/s/message": [FakeResponse(200, {"data": [
                {"type": "assistant", "model": {"id": "m"}, "content": [
                    {"type": "reasoning", "text": "先算 7*8"},
                    {"type": "text", "text": "56"},
                ]},
                {"type": "idle"},
            ]})],
            "/api/session/s": [FakeResponse(200, {})],
        })
        reply = await complete(client, _settings(), "q", model="m")
        assert reply.text == "56"
        assert "7*8" in reply.reasoning

    @pytest.mark.asyncio
    async def test_auth_error_is_not_reported_as_unreachable(self) -> None:
        """401 是**配置问题**（密码不对），报成「上游不可达」会把人带偏。"""
        client = FakeClient({
            "/api/session": [FakeResponse(401, {"_tag": "UnauthorizedError"})],
        })
        with pytest.raises(OpencodeError) as exc:
            await complete(client, _settings(), "q", model="m")
        assert "401" in str(exc.value)

    @pytest.mark.asyncio
    async def test_idle_without_content_is_an_error(self) -> None:
        """本轮结束但没内容 —— 多半是 opencode 侧在等权限确认。
        报「空回复」比回一个空字符串有用得多。"""
        client = FakeClient({
            "/api/session": [FakeResponse(200, {"data": {"id": "s"}})],
            "/api/session/s/prompt": [FakeResponse(200, {})],
            "/api/session/s/message": [FakeResponse(200, {"data": [{"type": "idle"}]})],
            "/api/session/s": [FakeResponse(200, {})],
        })
        with pytest.raises(OpencodeError, match="没有回复内容"):
            await complete(client, _settings(), "q", model="m")

    @pytest.mark.asyncio
    async def test_timeout_mentions_the_model(self) -> None:
        """超时信息要带模型名 —— 否则用户不知道是哪个模型卡住了。"""
        client = FakeClient({
            "/api/session": [FakeResponse(200, {"data": {"id": "s"}})],
            "/api/session/s/prompt": [FakeResponse(200, {})],
            "/api/session/s/message": [FakeResponse(200, {"data": []})],
            "/api/session/s": [FakeResponse(200, {})],
        })
        with pytest.raises(OpencodeError, match="big-pickle"):
            await complete(
                FakeClient(client.script), _settings(poll_timeout=0.01), "q", model="big-pickle"
            )


class TestPasswordDiscovery:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """读不到就返回 None（→ 不发认证头 → opencode 回 401），
        而不是抛异常把整个转发层带崩。"""
        assert discover_password(tmp_path / "nope.json") is None

    def test_broken_json_returns_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "service.json"
        bad.write_text("{not json", encoding="utf-8")
        assert discover_password(bad) is None

    def test_reads_the_password(self, tmp_path: Path) -> None:
        good = tmp_path / "service.json"
        good.write_text(json.dumps({"password": "s3cret"}), encoding="utf-8")
        assert discover_password(good) == "s3cret"

    def test_empty_password_is_treated_as_missing(self, tmp_path: Path) -> None:
        """空串不是有效凭证 —— 返回它会让 Basic 头变成 ``opencode:``，
        而那会被opencode 当成「密码为空」而不是「没设密码」。"""
        empty = tmp_path / "service.json"
        empty.write_text(json.dumps({"password": ""}), encoding="utf-8")
        assert discover_password(empty) is None

    def test_auth_header_is_basic_opencode(self) -> None:
        """认证头必须是 Basic base64("opencode:<pw>")——
        这是从 opencode 二进制里读到的确切实现（``Basic `` + base64）。
        """
        import base64

        settings = OpencodeSettings(password="secret")
        expected = "Basic " + base64.b64encode(b"opencode:secret").decode()
        assert settings.auth_header() == expected

    def test_auth_header_is_cached_per_instance(self, tmp_path: Path) -> None:
        """**必须缓存**：``_headers()`` 每个 HTTP 请求调一次，轮询期间每
        0.35 秒一次（120 秒 ≈ 340 次）。不缓存就是同步文件 IO 跑在事件循环上。

        做法是数 :func:`discover_password` 的调用次数。
        """
        calls: list[Path] = []
        real = oc.discover_password

        def counting(path: Path | None = None) -> str | None:
            calls.append(path or Path("?"))
            return real(path)

        oc.discover_password = counting  # type: ignore[assignment]
        try:
            settings = OpencodeSettings(password=None)
            for _ in range(5):
                settings.auth_header()
        finally:
            oc.discover_password = real
        assert len(calls) == 1, f"读了 {len(calls)} 次盘，应该只读 1 次"

    def test_replace_does_not_carry_a_stale_header(self) -> None:
        """``dataclasses.replace()`` **不能**把算过的认证头带过去。

        实测（第三轮 review 的 B-4）：``replace(inst, password="new")``
        会原样复制 ``_auth``，而 ``auth_header()`` 只在它是 ``None`` 时重算 ——
        于是改密码后不生效，与 docstring 承诺的「下次请求生效」矛盾。
        ``__post_init__`` 就是为这条存在的。
        """
        import base64
        import dataclasses

        def decoded(settings: OpencodeSettings) -> str:
            """解出 ``opencode:<password>`` 里的 password 部分。

            单独抽一个函数是为了让mypy 知道 ``auth_header()`` 不会是 ``None`` ——
            在测试里直接 ``.split()`` 会让mypy 报``Item "None" has no attribute``。
            """
            header = settings.auth_header()
            assert header is not None, "设了 password 却没算出认证头"
            return base64.b64decode(header.split()[1]).decode()

        orig = OpencodeSettings(password="old")
        assert decoded(orig) == "opencode:old"

        replaced = dataclasses.replace(orig, password="new")
        assert decoded(replaced) == "opencode:new", "认证头是陈旧的"

    def test_password_never_appears_in_repr(self) -> None:
        """明文口令绝不能进 ``repr()`` —— 它会出现在调试打印、异常回溯、
        测试失败输出里。"""
        settings = OpencodeSettings(password="secret-pw")
        settings.auth_header()
        assert "secret-pw" not in repr(settings)
        assert "Basic" not in repr(settings), "算出来的头也不该进 repr"
