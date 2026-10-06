"""每日「站外可达性」探测的调度。

与 :mod:`tests.test_container` 同理：这是一个「跑一次就没人在看」的循环，
可以坏掉而整套件照样绿。所以用**毫秒级时刻**把循环真驱动起来，
并把「对齐到凌晨 2 点」这件事本身也断言掉 ——
「每 24 小时跑一次」和「每天 2 点跑一次」在代码上只差一个数字，行为差得很远。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import pytest

from openproxy.config import Settings, load_settings
from openproxy.container import (
    REACHABILITY_HOUR,
    REACHABILITY_MINUTE,
    Container,
    seconds_until_next,
)
from openproxy.service.model_catalog import ProbeResult
from openproxy.store import Database, ProbeStore, Reachability, now_ms
from tests.support.upstream import FakeUpstream, Recorded, json_response, standard_upstream

TZ = 480


def stub_upstream(chat_status: int = 200) -> FakeUpstream:
    """清单里两个模型，够验证调度而不关心判定。"""
    return standard_upstream(
        **{
            "/v1/models": json_response(
                {"object": "list", "data": [{"id": "big-pickle"}, {"id": "space-bunny-free"}]}
            ),
            "/v1/chat/completions": httpx.Response(chat_status, json={}),
        }
    )


class Harness:
    """容器 + 它用的假上游。调度断言需要看「到底发出了几个请求」。"""

    def __init__(self, settings: Settings) -> None:
        self.upstream = stub_upstream()
        self.container = Container.build(
            settings, transport=self.upstream, tz_offset_minutes=TZ
        )

    @property
    def chat_calls(self) -> list[Recorded]:
        return [
            r
            for r in self.upstream.requests
            if r.url.endswith("/v1/chat/completions")
        ]

    def close(self) -> None:
        self.container.recorder.close()
        self.container.database.close()


@pytest.fixture
def harness(settings: Settings) -> Iterator[Harness]:
    h = Harness(settings)
    try:
        yield h
    finally:
        h.close()


@pytest.fixture
def prober(harness: Harness) -> Container:
    return harness.container


def _fresh() -> Reachability:
    """一个「刚刚才测过」的结果。

    用 ``now_ms()`` 而不是某个魔数：写死 ``10**12``（公元 33658 年）会让
    ``now - checked_at`` 变成负数，于是怎么都不过期 —— 测试看起来通过，
    实际把「过期该重扫」那条路整个走丢了。
    """
    return Reachability(status="ok", checked_at=now_ms())


# ------------------------------------------------------------- 对齐时刻 ---


class TestSecondsUntilNext:
    def test_later_today(self) -> None:
        now = datetime(2026, 10, 4, 0, 30)
        assert seconds_until_next(2, 0, now=now) == pytest.approx(5400, abs=1)

    def test_already_past_rolls_to_tomorrow(self) -> None:
        """已经过了今天的点就算到明天 —— 于是启动时正好是 3 点，
        会等到明天 2 点，不会在启动后一小时内把探测跑掉。"""
        now = datetime(2026, 10, 4, 3, 0)
        assert seconds_until_next(2, 0, now=now) == pytest.approx(23 * 3600, abs=1)

    def test_exactly_on_the_minute_rolls_forward(self) -> None:
        """整点整分时算「已经过了」，等一整天 —— 而不是睡0 秒后立刻再跑一次。"""
        now = datetime(2026, 10, 4, 2, 0, 0)
        assert seconds_until_next(2, 0, now=now) == pytest.approx(24 * 3600, abs=1)

    def test_one_second_before(self) -> None:
        now = datetime(2026, 10, 4, 1, 59, 59)
        assert seconds_until_next(2, 0, now=now) == pytest.approx(1, abs=1)

    def test_microseconds_are_dropped(self) -> None:
        """``replace(microsecond=0)`` 之后目标一定不带小数。

        不带小数才能保证「整点整分」的判断是精确的：带小数会让
        ``target <= current`` 在整点差一微秒时给出模棱两可的答案。
        """
        now = datetime(2026, 10, 4, 0, 0, 0, 999_999)
        seconds = seconds_until_next(2, 0, now=now)
        assert 7199 < seconds <= 7200
        # 整整两小时减去那一微秒
        assert seconds == pytest.approx(7199.000001, abs=0.001)

    def test_default_is_two_am(self) -> None:
        """用户明确要求「每天凌晨 2 点」。这一条把这个决定钉住。"""
        assert (REACHABILITY_HOUR, REACHABILITY_MINUTE) == (2, 0)


# ------------------------------------------------------------- 启动那一次 ---


class TestStartupProbe:
    async def test_runs_on_start_when_there_is_no_result(
        self, harness: Harness, prober: Container
    ) -> None:
        await prober.startup(start_pruner=False, start_prober=True)
        try:
            for _ in range(200):
                if prober.catalog.reachability:
                    break
                await asyncio.sleep(0.01)
            assert prober.catalog.reachability_of("big-pickle").status != "unknown"
        finally:
            await prober.shutdown()

    async def test_skips_when_the_stored_result_is_fresh(self, settings: Settings) -> None:
        """结果不到一天就不重发 —— 否则「每天 2 点」会变成「每次重启都发 10 个请求」。

        顺序要紧：**先把结果写进库，再启动容器** —— 这才是生产的真实顺序
        （``Catalog`` 在 ``Container.build`` 时一次性把库里的结论读进内存）。
        反过来写（先build 再写库）测的是一个生产里不存在的场景。
        """
        db = Database(Path(settings.db_path))
        db.migrate()
        ProbeStore(db).save({"big-pickle": _fresh()})
        db.close()

        h = Harness(settings)
        try:
            # 容器一启动就该已经读到旧结论
            assert h.container.catalog.reachability_of("big-pickle").status == "ok"
            await h.container.startup(start_pruner=False, start_prober=True)
            try:
                await asyncio.sleep(0.2)
                assert h.chat_calls == [], "新鲜的结论不该被重跑覆盖"
            finally:
                await h.container.shutdown()
        finally:
            h.close()

    async def test_off_by_switch(self, harness: Harness, prober: Container) -> None:
        await prober.startup(start_pruner=False, start_prober=False)
        try:
            await asyncio.sleep(0.1)
            assert prober._probe_task is None
            assert prober.catalog.reachability == {}
            assert harness.chat_calls == []
        finally:
            await prober.shutdown()

    async def test_follows_the_settings_flag_by_default(self, settings: Settings) -> None:
        """``start_prober=None``（生产路径）要跟随 settings.probe_reachability。"""
        off = Settings(db_path=settings.db_path, probe_reachability=False)
        c = Container.build(off, transport=stub_upstream(), tz_offset_minutes=TZ)
        try:
            await c.startup(start_pruner=False)
            assert c._probe_task is None
        finally:
            c.recorder.close()
            c.database.close()

    async def test_result_is_written_to_the_database(
        self, prober: Container, settings: Settings
    ) -> None:
        """落库是硬需求：只在内存里的话，重启后控制台又变回「未探测」。"""
        await prober.startup(start_pruner=False, start_prober=True)
        try:
            for _ in range(200):
                if prober.probes.load():
                    break
                await asyncio.sleep(0.01)
            stored = prober.probes.load()
            assert stored, "探测结果没有落库"
            assert set(stored) <= {"big-pickle", "space-bunny-free"}
        finally:
            await prober.shutdown()

        # 换一个进程视角：新建容器必须读得到
        db = Database(Path(settings.db_path))
        db.migrate()
        try:
            assert ProbeStore(db).load()
        finally:
            db.close()


# ------------------------------------------------------------- 每日那一拍 ---

class TestDailyLoop:
    """「每天凌晨 2 点」这个循环本身。

    全部用注入的 sleep 把 14 小时缩成几毫秒 —— 这是 _reachability_loop
    那个 sleep 参数存在的**唯一理由**：不注入的话，「循环真的自己跑了一拍」
    这条断言在测试里永远无法成立（要真等 14 小时）。
    """

    async def test_loop_actually_fires_on_its_own(self, prober: Container) -> None:
        rounds = {"n": 0}
        original = prober.catalog.probe

        async def counting(*args: Any, **kwargs: Any) -> ProbeResult:
            rounds["n"] += 1
            return await original(*args, **kwargs)

        async def instant(_seconds: float) -> None:
            await asyncio.sleep(0)

        with mock.patch.object(prober.catalog, "probe", counting):
            task = asyncio.create_task(
                prober._reachability_loop(run_on_start=False, sleep=instant)
            )
            try:
                for _ in range(400):
                    if rounds["n"] >= 2:
                        break
                    await asyncio.sleep(0.01)
                assert rounds["n"] >= 2, "循环没有自己触发多轮探测"
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    async def test_it_waits_for_the_aligned_moment(self, prober: Container) -> None:
        """循环必须先等「到下一个 02:00」再跑，不能连转。

        这条钉住「每天 2 点」而不是「每 N 小时一次」—— 后者在日志里看起来一样，
        但只有对齐的那个能回答「昨天凌晨那次扫的结果是什么」。
        """
        waited: list[float] = []

        async def record(seconds: float) -> None:
            waited.append(seconds)
            await asyncio.sleep(0)

        task = asyncio.create_task(
            prober._reachability_loop(run_on_start=False, sleep=record)
        )
        try:
            for _ in range(200):
                if waited:
                    break
                await asyncio.sleep(0.01)
            assert waited, "循环没有等过"
            assert waited[0] == pytest.approx(seconds_until_next(2, 0), abs=2)
            assert 0 < waited[0] <= 24 * 3600
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_one_failure_does_not_kill_the_loop(self, prober: Container) -> None:
        """一次失败不代表明天不该再试 —— 长期观测任务不能被一次抖动杀死。"""
        calls = {"n": 0}

        async def boom(*_args: Any, **_kwargs: Any) -> ProbeResult:
            calls["n"] += 1
            raise RuntimeError("upstream down")

        async def instant(_seconds: float) -> None:
            await asyncio.sleep(0)

        with mock.patch.object(prober.catalog, "probe", boom):
            task = asyncio.create_task(
                prober._reachability_loop(run_on_start=False, sleep=instant)
            )
            try:
                for _ in range(400):
                    if calls["n"] >= 2:
                        break
                    await asyncio.sleep(0.01)
                assert calls["n"] >= 2, "第一拍失败之后循环就死了"
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    async def test_start_up_probe_runs_before_the_first_wait(
        self, prober: Container
    ) -> None:
        """结果过期时，启动那一刻就该扫一次，而不是等到凌晨 2 点。"""
        calls = {"n": 0}
        original = prober.catalog.probe

        async def counting(*args: Any, **kwargs: Any) -> ProbeResult:
            calls["n"] += 1
            return await original(*args, **kwargs)

        async def instant(_seconds: float) -> None:
            await asyncio.sleep(0)

        with mock.patch.object(prober.catalog, "probe", counting):
            task = asyncio.create_task(prober._reachability_loop(sleep=instant))
            try:
                for _ in range(200):
                    if calls["n"] >= 1:
                        break
                    await asyncio.sleep(0.01)
                assert calls["n"] >= 1
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    async def test_upstream_unreachable_is_logged_not_raised(
        self, settings: Settings
    ) -> None:
        """上游不可达时只是记一条日志，不能把后台任务炸掉。"""

        def dead(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=_request)

        c = Container.build(
            settings,
            transport=httpx.MockTransport(dead),
            tz_offset_minutes=TZ,
        )
        try:
            await c._run_reachability()  # 不抛就算过
        finally:
            c.recorder.close()
            c.database.close()


# ---------------------------------------------------------------- 关停 ---


class TestProberShutdown:
    async def test_shutdown_cancels_the_prober_promptly(self, harness: Harness) -> None:
        """关停必须立刻完成：探测循环里睡到明天 2 点，不主动取消就会拖满超时预算。"""
        c = harness.container
        await c.startup(start_pruner=False, start_prober=True)
        task = c._probe_task
        assert task is not None

        started = time.monotonic()
        await asyncio.wait_for(c.shutdown(), timeout=5)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"关停耗时 {elapsed:.2f}s，说明没主动取消探测任务"
        assert task.cancelled()
        assert c._probe_task is None

    async def test_shutdown_with_both_loops(self, harness: Harness) -> None:
        c = harness.container
        await c.startup(start_pruner=True, start_prober=True)
        await asyncio.wait_for(c.shutdown(), timeout=5)
        assert c._prune_task is None
        assert c._probe_task is None
        assert c.database.open_connections == 0


def test_env_switch_exists() -> None:
    """开关必须有环境变量入口，否则关不掉。"""
    assert load_settings({"OPENPROXY_PROBE_REACHABILITY": "false"}).probe_reachability is False
    assert load_settings({"OPENPROXY_PROBE_REACHABILITY": "0"}).probe_reachability is False
    assert load_settings({}).probe_reachability is True
