"""站外可达性探测。

单独一个文件，因为它是本项目里**唯一会主动消耗上游额度**的功能：测错了就是
真花掉额度、测漏了就是控制台骗人「上游在线」。所以边界比别处密。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest import mock

import httpx
import pytest

from openproxy.service.model_catalog import (
    REACHABILITY_TTL_MS,
    ModelCatalog,
    classify_probe_response,
)
from openproxy.store import Database, ProbeStore, Reachability

BASE = "https://example.test/zen"

#: 实测上游的拒绝报文（2026-10-04）。逐字照抄，因为判定要靠它。
FREE_TIER_BODY = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "FreeTierError",
            "message": (
                "Error from provider (Console): OpenCode's free tier can only be "
                "used from within OpenCode"
            ),
        },
    }
).encode()


def catalog_response(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"id": i} for i in ids]})


class Recorder:
    """记录所有出站请求的假上游。"""

    def __init__(self, chat_status: int = 200, chat_body: bytes = b"{}") -> None:
        self.chat_status = chat_status
        self.chat_body = chat_body
        self.models_calls = 0
        self.chat_models: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            self.models_calls += 1
            return catalog_response("space-bunny-free", "big-pickle")
        body = json.loads(request.content or b"{}")
        self.chat_models.append(str(body.get("model")))
        return httpx.Response(self.chat_status, content=self.chat_body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


# --------------------------------------------------------------- 判定 ---


class TestClassify:
    def test_2xx_is_ok(self) -> None:
        assert classify_probe_response(200, b"{}")[0] == "ok"

    def test_free_tier_error_is_blocked_not_unknown(self) -> None:
        """实测 10 个免费模型里 9 个是这个形状。

        必须判成 ``blocked`` 而不是 ``unknown`` —— 前者是「上游明确不许」，
        后者是「本站不知道」，混了会让一次网络抖动把结论冲掉。
        """
        status, detail = classify_probe_response(403, FREE_TIER_BODY)
        assert status == "blocked"
        assert "OpenCode 客户端" in detail

    def test_message_marker_matches_even_without_exact_type(self) -> None:
        """按文案兜底：上游改类型名但文案还在时仍要认出来。"""
        body = b'{"error":{"message":"free tier can only be used from within OpenCode"}}'
        assert classify_probe_response(403, body)[0] == "blocked"

    def test_other_401_is_blocked(self) -> None:
        assert classify_probe_response(401, b"")[0] == "blocked"

    def test_upstream_5xx_is_unknown_not_blocked(self) -> None:
        """上游自己挂了 ≠ 拒绝本站。把 5xx 记成 blocked 会误伤 10 个模型。"""
        assert classify_probe_response(503, b"")[0] == "unknown"

    def test_garbage_body_does_not_crash(self) -> None:
        for body in (b"", b"<html/>", b"\xff\xfe\x00", FREE_TIER_BODY[:100]):
            assert classify_probe_response(500, body)[0] == "unknown"

    def test_marker_in_a_5xx_body_is_still_unknown(self) -> None:
        """文案标记只在 4xx 上认。

        这条是被测试逼出来的真缺陷：原先 5xx 也扫正文，于是上游网关只要在
        错误页里带上那串字（例如整页包着「free tier can only be used from
        within OpenCode」），一个 503 就会被记成「上游明确拒绝」——
        而 5xx 的正确处置是「上游自己坏了，等明天再扫」。
        """
        assert classify_probe_response(503, FREE_TIER_BODY)[0] == "unknown"


# --------------------------------------------------------------- 探测 ---


class TestProbeReachability:
    async def test_blocked_model_is_marked_blocked(self) -> None:
        rec = Recorder(chat_status=403, chat_body=FREE_TIER_BODY)
        c = ModelCatalog()
        async with rec.client() as client:
            await c.probe(client, BASE, reachability=True, gap=0)
        assert c.reachability_of("big-pickle").status == "blocked"
        assert c.reachability_of("big-pickle").status_code == 403

    async def test_ok_model_is_marked_ok(self) -> None:
        rec = Recorder(chat_status=200)
        c = ModelCatalog()
        async with rec.client() as client:
            await c.probe(client, BASE, reachability=True, gap=0)
        assert c.reachability_of("big-pickle").status == "ok"

    async def test_only_catalog_models_are_probed(self) -> None:
        """清单外的模型本来就不会被转发，测它是纯浪费额度。"""
        rec = Recorder()
        c = ModelCatalog()
        async with rec.client() as client:
            await c.probe(client, BASE, reachability=True, gap=0)
        # /v1/models 只返回两个 id，所以只该测这两个，而不是清单里的 10 个
        assert set(rec.chat_models) == {"space-bunny-free", "big-pickle"}

    async def test_off_by_default_because_it_costs_quota(self) -> None:
        """默认必须不发那 10 个请求 —— 这是额度开关，不是顺手开的。"""
        rec = Recorder()
        c = ModelCatalog()
        async with rec.client() as client:
            await c.probe(client, BASE)
        assert rec.chat_models == []
        assert rec.models_calls == 1

    async def test_network_failure_is_unknown_not_blocked(self) -> None:
        """一次断网不该把 10 个模型误标成「上游拒绝」。"""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v1/models"):
                return catalog_response("big-pickle")
            raise httpx.ConnectError("boom", request=request)

        c = ModelCatalog()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await c.probe(client, BASE, reachability=True, gap=0)
        assert c.reachability_of("big-pickle").status == "unknown"
        assert "网络错误" in c.reachability_of("big-pickle").detail

    async def test_one_model_failure_does_not_abort_the_round(self) -> None:
        """单个模型的抖动不该让整轮探测报废 —— 剩下 9 个仍然要测。"""
        state = {"n": 0}
        failed_model = ""

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal failed_model
            if request.url.path.endswith("/v1/models"):
                return catalog_response("space-bunny-free", "big-pickle")
            body = json.loads(request.content or b"{}")
            model = str(body.get("model"))
            state["n"] += 1
            if state["n"] == 1:
                failed_model = model
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json={})

        c = ModelCatalog()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await c.probe(client, BASE, reachability=True, gap=0)
        reach = c.reachability
        # 不假设谁先被测：探测按 FREE_MODELS 的顺序，不是 /v1/models 的返回顺序
        assert reach[failed_model].status == "unknown"
        assert len(reach) == 2
        assert sum(1 for r in reach.values() if r.status == "ok") == 1

    async def test_probes_are_serialised_with_a_gap(self) -> None:
        """并发会被上游按突发判定；间隔必须是**真的 sleep**，不能是空转。"""
        gaps: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds: float) -> None:
            gaps.append(seconds)
            await real_sleep(0)

        order: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v1/models"):
                return catalog_response("space-bunny-free", "big-pickle")
            order.append("chat")
            return httpx.Response(200, json={})

        c = ModelCatalog()
        with mock.patch("openproxy.service.model_catalog.asyncio.sleep", fake_sleep):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await c.probe(client, BASE, reachability=True, gap=1.5)
        assert order == ["chat", "chat"]
        # 第一个之前不 sleep，之后每个之间都要有 —— 少了间隔就是并发打上游
        assert gaps == [1.5]


class TestStaleness:
    def test_no_result_means_stale(self) -> None:
        assert ModelCatalog().is_stale(now_ms=1_000_000) is True

    def test_fresh_result_is_not_stale(self) -> None:
        c = ModelCatalog()
        c._reach["big-pickle"] = Reachability(status="ok", checked_at=1_000_000)
        assert c.is_stale(now_ms=1_000_000 + REACHABILITY_TTL_MS - 1) is False

    def test_expired_result_is_stale(self) -> None:
        """一天后必须重扫 —— 上游的策略会变，本站的观测不能是永久快照。"""
        c = ModelCatalog()
        c._reach["big-pickle"] = Reachability(status="ok", checked_at=1_000_000)
        assert c.is_stale(now_ms=1_000_000 + REACHABILITY_TTL_MS) is True

    def test_uses_the_newest_timestamp_not_the_oldest(self) -> None:
        c = ModelCatalog()
        c._reach["a"] = Reachability(status="ok", checked_at=1_000)
        c._reach["b"] = Reachability(status="ok", checked_at=9_000_000)
        assert c.is_stale(now_ms=9_000_001) is False


# --------------------------------------------------------------- 落库 ---


class TestProbeStore:
    def test_round_trip(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "p.db")
        db.migrate()
        store = ProbeStore(db)
        store.save(
            {
                "big-pickle": Reachability(
                    status="blocked", checked_at=123, detail="上游拒绝", status_code=403
                ),
                "space-bunny-free": Reachability(status="ok", checked_at=123),
            }
        )
        loaded = ProbeStore(db).load()
        assert loaded["big-pickle"].status == "blocked"
        assert loaded["big-pickle"].detail == "上游拒绝"
        assert loaded["big-pickle"].status_code == 403
        assert loaded["space-bunny-free"].status == "ok"
        db.close()

    def test_survives_a_restart(self, tmp_path: Path) -> None:
        """进程重启后控制台不能又变回「未探测」—— 用户会以为刚才看错了。"""
        path = tmp_path / "p.db"
        db = Database(path)
        db.migrate()
        ProbeStore(db).save({"big-pickle": Reachability(status="blocked", checked_at=1)})
        db.close()

        db2 = Database(path)
        db2.migrate()
        assert ModelCatalog(store=ProbeStore(db2)).reachability_of("big-pickle").status == (
            "blocked"
        )
        db2.close()

    def test_missing_key_is_empty_not_an_error(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "p.db")
        db.migrate()
        assert ProbeStore(db).load() == {}
        db.close()

    @pytest.mark.parametrize(
        "corrupt",
        ["not json", "[]", '"a string"', '{"a": 123}', '{"a": {"status": "ok"}}'],
    )
    def test_corrupt_payload_degrades_to_unknown(
        self, tmp_path: Path, corrupt: str
    ) -> None:
        """``kv`` 表没有约束，写坏了不能让整个控制台 500。"""
        db = Database(tmp_path / "p.db")
        db.migrate()
        db.kv_set("model_reachability", corrupt)
        loaded = ProbeStore(db).load()
        for value in loaded.values():
            assert value.status == "unknown"
        db.close()

    def test_long_detail_is_capped(self) -> None:
        """正文被上游撑到很大时不能原样进库。"""
        r = Reachability(status="blocked", detail="x" * 5000)
        assert len(Reachability.from_json(r.to_json()).detail) <= 300

    def test_result_is_merged_not_replaced_by_a_partial_round(self, tmp_path: Path) -> None:
        """一轮只测到部分模型时，不能把上一轮的结论抹掉。"""
        db = Database(tmp_path / "p.db")
        db.migrate()
        store = ProbeStore(db)
        store.save({"a": Reachability(status="blocked"), "b": Reachability(status="ok")})
        store.save({"a": Reachability(status="blocked")})
        assert set(store.load()) == {"a"}
        db.close()


class TestModelReachability:
    async def test_result_reaches_the_catalog_entries(self) -> None:
        """卡片上的标记必须来自探测结果，而不是清单里的静态快照。"""
        rec = Recorder(chat_status=403, chat_body=FREE_TIER_BODY)
        c = ModelCatalog()
        async with rec.client() as client:
            await c.probe(client, BASE, reachability=True, gap=0)
        entries = {e.model.model_id: e for e in c.entries()}
        assert entries["big-pickle"].reachability is not None
        assert entries["big-pickle"].reachability.status == "blocked"
        # 没探测到的模型是 None（前端显示「可达性未探测」），不是 unknown 字符串
        assert entries["ling-3.1-flash-free"].reachability is None
