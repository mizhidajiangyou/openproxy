"""强制注入 ``reasoning_effort``。

三处都要测：纯函数、配置合成、真实转发链路 —— 少任何一层就会出现
「设置页开关点得动但转发没变」这种中间产物正确、可观测行为错误的情况。
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openproxy.config import Overlays, RuntimeConfig, Settings, load_settings
from openproxy.container import Container
from openproxy.domain import VALID_REASONING_EFFORTS
from openproxy.service.usage_extract import (
    REASONING_EFFORTS,
    inject_reasoning_effort,
)
from tests.support.upstream import FakeUpstream, standard_upstream

BODY = {"model": "space-bunny-free", "messages": [{"role": "user", "content": "hi"}]}


def body_with(**extra: object) -> bytes:
    return json.dumps({**BODY, **extra}).encode()


def effort_of(raw: bytes) -> str | None:
    value = json.loads(raw).get("reasoning_effort")
    return value if isinstance(value, str) else None


# --------------------------------------------------------------- 纯函数 ---


class TestInjectReasoningEffort:
    @pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
    def test_writes_the_value(self, effort: str) -> None:
        assert effort_of(inject_reasoning_effort(body_with(), effort)) == effort

    def test_overwrites_the_client_value(self) -> None:
        """语义是**强制**不是「兜底」：客户端写了 low、开成 high，必须是 high。"""
        out = inject_reasoning_effort(body_with(reasoning_effort="low"), "high")
        assert effort_of(out) == "high"

    def test_is_idempotent(self) -> None:
        once = inject_reasoning_effort(body_with(), "medium")
        assert inject_reasoning_effort(once, "medium") == once

    @pytest.mark.parametrize("effort", ["none", "minimal", "auto", "", "HIGH", "高"])
    def test_rejects_values_upstream_does_not_accept(self, effort: str) -> None:
        """不认的值必须原样返回 —— 写上去会让用户的请求从 200 变 400。"""
        raw = body_with()
        assert inject_reasoning_effort(raw, effort) == raw

    def test_preserves_the_rest_of_the_body(self) -> None:
        """只改一个键，别把 messages / stream 之类弄坏。"""
        raw = body_with(stream=True, temperature=0.5)
        out = json.loads(inject_reasoning_effort(raw, "low"))
        assert out["messages"] == BODY["messages"]
        assert out["stream"] is True
        assert out["temperature"] == 0.5

    @pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'"str"', b"\xff\xfe"])
    def test_non_json_bodies_pass_through(self, raw: bytes) -> None:
        """改不动就原样返回：宁可少一个功能，也不能弄坏用户的请求。"""
        assert inject_reasoning_effort(raw, "low") == raw

    def test_utf8_is_not_escaped(self) -> None:
        """``ensure_ascii=False``：中文提示词被转成 \\uXXXX 会无谓地膨胀正文。"""
        raw = json.dumps(
            {"model": "space-bunny-free", "messages": [{"role": "user", "content": "你好"}]},
            ensure_ascii=False,
        ).encode()
        assert "你好".encode() in inject_reasoning_effort(raw, "low")

    def test_effort_list_has_no_silent_off_values(self) -> None:
        """``none`` / ``minimal`` 上游返回 200 但 usage 为空 —— 等于静默关掉思考。

        把它们列为合法档位会让人以为思考还开着。所以这张表只有四档。
        """

        assert set(REASONING_EFFORTS) == {"low", "medium", "high", "max"}
        assert "none" not in REASONING_EFFORTS
        assert "minimal" not in REASONING_EFFORTS

    def test_max_is_accepted_by_the_wire(self) -> None:
        """``max`` 是实测存在的一档，不能只在UI 里出现。

        这条守的是「别再把 max 删掉」：它一度因为没被实测过而不在这张表里，
        于是下拉框选 max → ``effortPreset()`` 的白名单漏了它 → 静默降级成 low
        （用户选了拉满、拿到最低档，且无任何报错）。
        """
        raw = json.dumps({"model": "m", "messages": []}).encode()
        out = inject_reasoning_effort(raw, "max")
        sent = json.loads(out)
        assert sent["reasoning_effort"] == "max"

    def test_max_survives_settings_validation(self) -> None:
        """配置层必须接受 max（否则环境变量与 PATCH 都会被拒）。"""
        assert load_settings({"OPENPROXY_REASONING_EFFORT": "max"}).reasoning_effort == "max"

    def test_service_and_domain_agree(self) -> None:
        """两个常量指向同一份事实（配置层用领域层那份校验）。"""
        assert REASONING_EFFORTS == VALID_REASONING_EFFORTS


# --------------------------------------------------------------- 配置层 ---


class TestEffortConfig:
    def test_default_is_off(self) -> None:
        assert load_settings({}).reasoning_effort is None

    def test_env_value_is_read(self) -> None:
        s = load_settings({"OPENPROXY_REASONING_EFFORT": "medium"})
        assert s.reasoning_effort == "medium"

    @pytest.mark.parametrize("bad", ["none", "minimal", "auto", "HIGH"])
    def test_bad_env_value_is_rejected_at_construction(self, bad: str) -> None:
        from openproxy.config import ConfigError

        with pytest.raises(ConfigError):
            load_settings({"OPENPROXY_REASONING_EFFORT": bad})

    def test_blank_env_value_means_unset(self) -> None:
        """空白 = 未设置，不是非法值。

        这是全配置层统一的约定（``PORT=""`` 在容器编排里很常见，按字面解释会得到
        「监听空地址」这种启动后才崩的坏配置），所以 ``"  "`` 返回 None 而不是报错。
        """
        assert load_settings({"OPENPROXY_REASONING_EFFORT": "   "}).reasoning_effort is None

    def test_overlay_wins_over_env(self) -> None:
        base = load_settings({"OPENPROXY_REASONING_EFFORT": "low"})
        rc = RuntimeConfig.compose(base, Overlays(reasoning_effort="high"))
        assert rc.reasoning_effort == "high"

    def test_blank_overlay_falls_back_to_the_env_baseline(self) -> None:
        """留空 = **「显式关闭强制」**，与环境变量基线无关。

        曾经的语义是「留空 = 我不覆盖，回环境变量的值」，与 ``upstream_base``
        同一套。但那让**「在界面关掉开关」这个动作无法表达**：``None`` 的含义
        是「没设过」，于是环境变量里设过的值会复活 —— 实测
        ``OPENPROXY_REASONING_EFFORT=high`` 时提交空串，生效值仍是 ``high``，
        而界面显示已关闭、**没有任何提示**。而前端明确承诺
        「关：完全不动客户端的 body，由客户端自己决定」。

        所以现在是三态，与 ``opencode_models`` 同一套：
        ``None`` = 没设过（回基线）、``""`` = 显式关闭（永不注入）。
        """
        base = load_settings({"OPENPROXY_REASONING_EFFORT": "high"})
        rc = RuntimeConfig.compose(base, Overlays(reasoning_effort="   ").validated())
        assert rc.reasoning_effort is None, "空串必须压过环境变量基线"

        # 没有环境变量兜底时，留空同样是「不注入」
        rc2 = RuntimeConfig.compose(load_settings({}), Overlays(reasoning_effort="  ").validated())
        assert rc2.reasoning_effort is None

    def test_blank_survives_a_restart(self) -> None:
        """**空串必须能编解码往返**，否则「关掉开关」重启后就被撤销。

        ``decode_overlays`` 曾用 ``read_str``（``.strip() or None``）读它，
        把 ``""`` 读成 ``None`` = 「没设过」→ 环境变量复活 → bug 复现。
        这条测试就是钉住那个 ``read_str_or_blank`` 分支的。
        """
        from openproxy.service.config_service import decode_overlays, encode_overlays

        base = load_settings({"OPENPROXY_REASONING_EFFORT": "high"})
        overlay = Overlays(reasoning_effort="  ").validated()
        assert overlay.reasoning_effort == ""

        revived = decode_overlays(encode_overlays(overlay))
        assert revived.reasoning_effort == "", "空串被读成了 None，重启后会失效"
        rc = RuntimeConfig.compose(base, revived)
        assert rc.reasoning_effort is None

    def test_unset_still_falls_back_to_baseline(self) -> None:
        """回归：``None``（键不存在）仍然回环境变量基线 —— 三态不能塌成两态。"""
        base = load_settings({"OPENPROXY_REASONING_EFFORT": "high"})
        assert RuntimeConfig.compose(base, Overlays()).reasoning_effort == "high"
        assert RuntimeConfig.compose(base, Overlays(reasoning_effort=None)).reasoning_effort == "high"

    def test_overlay_is_trimmed(self) -> None:
        base = load_settings({})
        rc = RuntimeConfig.compose(base, Overlays(reasoning_effort="  low  ").validated())
        assert rc.reasoning_effort == "low"

    def test_public_dict_exposes_the_allowed_values(self) -> None:
        """前端要渲染下拉框，合法档位必须由后端给，不能在前端再写死一份。"""
        payload = RuntimeConfig.compose(load_settings({})).public_dict()
        assert payload["reasoning_efforts"] == list(VALID_REASONING_EFFORTS)
        assert payload["reasoning_effort"] is None

    def test_overlay_codec_round_trip(self) -> None:
        from openproxy.service.config_service import decode_overlays, encode_overlays

        original = Overlays(reasoning_effort="high")
        assert decode_overlays(encode_overlays(original)) == original

    def test_unknown_field_is_rejected(self) -> None:
        from openproxy.config import ConfigError

        with pytest.raises(ConfigError):
            Overlays().patch(reasoning_effortt="high")


# --------------------------------------------------------------- 转发层 ---
#
# ``_app`` 建出来的 app 各自持有一个 SQLite 连接与一个写线程。Python 3.14 的
# ``sqlite3`` 会为未关闭的连接发 ``ResourceWarning``（本项目 filterwarnings=error），
# 而漏关的后果不是「这个用例挂了」而是**失败随机漂移到别的用例** —— GC 在哪个
# 用例期间回收就记到哪个头上。所以这里统一登记、夹具末尾统一关。
_APPS: list[FastAPI] = []


@pytest.fixture(autouse=True)
def _close_built_apps() -> Iterator[None]:
    """用例结束时关掉本文件所有 ``_app()`` 出来的 app。"""
    _APPS.clear()
    yield
    while _APPS:
        app = _APPS.pop()
        # ``app.state.container`` 是 Starlette 的 State（一个属性字典），
        # 静态类型看不到我们塞进去的键，所以这里要 cast 而不是 ignore。
        container = cast(Container, app.state.container)
        with contextlib.suppress(Exception):
            container.recorder.close()
        with contextlib.suppress(Exception):
            container.database.close()


def _app(settings: Settings, upstream: FakeUpstream) -> FastAPI:
    from openproxy.app import create_app

    app = create_app(
        settings,
        transport=upstream,
        start_pruner=False,
        start_prober=False,
        tz_offset_minutes=480,
    )
    _APPS.append(app)
    return app


@pytest.fixture
def wire_settings(tmp_path: Path) -> Settings:
    """转发链路用的配置。

    **必须给临时库**：``load_settings({})`` 会拿到 ``data/openproxy.db`` ——
    也就是**正在跑的那个库**。于是每个用例都在往生产库里写测试记录，而且多个
    用例共用同一个文件；Python 3.14 的 sqlite3 会为最终被 GC 的那条连接发
    ``ResourceWarning``，而 ``filterwarnings = error`` 把它变成失败 ——
    症状是「失败随机漂移到不同用例」（实测连跑三次漂了两次）。
    """
    return dataclasses.replace(load_settings({}), db_path=str(tmp_path / "wire.db"))


class TestEffortOnTheWire:
    def _post(self, client: TestClient, upstream: FakeUpstream, **body: object) -> dict[str, object]:
        response = client.post("/v1/chat/completions", json={**BODY, **body})
        assert response.status_code == 200, response.text
        assert upstream.requests, "请求没有到达上游"
        sent: dict[str, object] = json.loads(upstream.requests[-1].content)
        return sent

    def test_injected_when_enabled(self, wire_settings: Settings) -> None:
        upstream = standard_upstream()
        tuned = dataclasses.replace(wire_settings, reasoning_effort="high")
        with TestClient(_app(tuned, upstream)) as c:
            sent = self._post(c, upstream)
        assert sent["reasoning_effort"] == "high"

    def test_absent_when_disabled(self, wire_settings: Settings) -> None:
        """默认关闭时必须**一个字节都不改** —— 这是纯反向代理的默认行为。"""
        upstream = standard_upstream()
        with TestClient(_app(wire_settings, upstream)) as c:
            sent = self._post(c, upstream)
        assert "reasoning_effort" not in sent

    def test_client_value_survives_when_disabled(self, wire_settings: Settings) -> None:
        """关掉开关后客户端自己写的值要原样透传，不能被本站清掉。"""
        upstream = standard_upstream()
        with TestClient(_app(wire_settings, upstream)) as c:
            sent = self._post(c, upstream, reasoning_effort="low")
        assert sent["reasoning_effort"] == "low"

    def test_get_requests_are_untouched(self, wire_settings: Settings) -> None:
        """``GET /v1/models`` 没有 body，注入不能凭空造一个。"""
        upstream = standard_upstream()
        tuned = dataclasses.replace(wire_settings, reasoning_effort="high")
        with TestClient(_app(tuned, upstream)) as c:
            assert c.get("/v1/models").status_code == 200
        assert upstream.requests[-1].method == "GET"
        assert upstream.requests[-1].content == b""

    def test_stacks_with_stream_usage_injection(self, wire_settings: Settings) -> None:
        """两个改写函数叠在一起不能互相覆盖 —— 这是白送的一类bug。"""
        upstream = standard_upstream()
        tuned = dataclasses.replace(
            wire_settings, reasoning_effort="low", inject_stream_usage=True
        )
        with TestClient(_app(tuned, upstream)) as c:
            sent = self._post(c, upstream, stream=True)
        assert sent["reasoning_effort"] == "low"
        options = sent["stream_options"]
        assert isinstance(options, dict)
        assert options["include_usage"] is True

    def test_stacks_with_model_normalisation(self, wire_settings: Settings) -> None:
        upstream = standard_upstream()
        tuned = dataclasses.replace(wire_settings, reasoning_effort="high")
        with TestClient(_app(tuned, upstream)) as c:
            sent = self._post(c, upstream, model=" space-bunny-free ")
        assert sent["reasoning_effort"] == "high"
        assert sent["model"] == "space-bunny-free"

    def test_usage_still_recorded(self, client: TestClient) -> None:
        """注入不能干扰用量抽取 —— 强制思考会让 completion_tokens 变大。"""
        response = client.post("/v1/chat/completions", json=BODY)
        assert response.status_code == 200
        usage = response.json()["usage"]
        assert usage["total_tokens"] > 0


class TestEffortApiContract:
    def test_patch_turns_it_on(self, client: TestClient) -> None:
        assert client.patch("/api/admin/settings", json={"reasoning_effort": "high"}).status_code == 200
        assert client.get("/api/admin/settings").json()["reasoning_effort"] == "high"

    def test_patch_blank_cancels_it(self, client: TestClient) -> None:
        client.patch("/api/admin/settings", json={"reasoning_effort": "high"})
        response = client.patch("/api/admin/settings", json={"reasoning_effort": ""})
        assert response.status_code == 200, response.text
        assert client.get("/api/admin/settings").json()["reasoning_effort"] is None

    def test_patch_null_means_no_change(self, client: TestClient) -> None:
        """null 是「不修改」，空串才是「取消强制」—— 两者语义不能混。"""
        client.patch("/api/admin/settings", json={"reasoning_effort": "high"})
        assert client.patch("/api/admin/settings", json={"reasoning_effort": None}).status_code == 400
        assert client.get("/api/admin/settings").json()["reasoning_effort"] == "high"

    @pytest.mark.parametrize("bad", ["none", "minimal", "auto", "x" * 40])
    def test_patch_rejects_bad_values(self, client: TestClient, bad: str) -> None:
        assert client.patch("/api/admin/settings", json={"reasoning_effort": bad}).status_code == 422

    def test_persists_across_restart(self, settings: Settings) -> None:
        """覆盖层必须落库，否则「重启后设置丢了」比开关不生效更让人困惑。"""
        from openproxy.service.config_service import ConfigService
        from openproxy.store import Database

        db = Database(Path(settings.db_path))
        db.migrate()
        try:
            ConfigService(db, settings).patch(reasoning_effort="medium")
        finally:
            db.close()

        db2 = Database(Path(settings.db_path))
        db2.migrate()
        try:
            rc = ConfigService(db2, settings).snapshot
            assert rc.reasoning_effort == "medium"
        finally:
            db2.close()
