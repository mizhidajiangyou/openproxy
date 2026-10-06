"""容器的生命周期契约：启动清理、后台清理循环、关停。

这几个方法在生产里是「跑一次就再也不被人看一眼」的那种代码 —— 单元测试天然
不覆盖，于是它们可以坏掉而整套件照样绿。所以这里用**毫秒级间隔**把循环真的
驱动起来，并把每一拍的可见结果都断言掉。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from openproxy.config import Settings
from openproxy.container import PRUNE_INTERVAL_SECONDS, Container
from openproxy.domain import TokenUsage, UsageRecord
from openproxy.store import DAY_MS, UsageStore, day_start_ms, now_ms
from tests.support.upstream import standard_upstream

TZ = 480


def make_record(days_ago: int) -> UsageRecord:
    ts = day_start_ms(now_ms(), TZ) - days_ago * DAY_MS + 3_600_000
    return UsageRecord(
        ts=ts,
        model="space-bunny-free",
        path="/v1/chat/completions",
        stream=False,
        status=200,
        latency_ms=1,
        usage=TokenUsage(1, 0, 0, 0, 1, True),
    )


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    c = Container.build(settings, transport=standard_upstream(), tz_offset_minutes=TZ)
    try:
        yield c
    finally:
        c.recorder.close()
        c.database.close()


class TestHttpClientIgnoresProxyEnv:
    """``trust_env=False``：**本机直连不能被 ``http_proxy`` 环境变量绕进代理。**

    ## 这是一个真实踩过的坑（2026-10-05）

    httpx 的 ``trust_env`` 默认是 ``True`` —— 会读 ``http_proxy`` /
    ``https_proxy`` / ``ALL_PROXY``。而用户的运行环境里正好有
    ``http_proxy=http://127.0.0.1:50572``，于是**连本机的 opencode
    （``127.0.0.1:4096``）都被转发给那个代理**，代理连不上就回
    ``502 upstream connect failed: Connection refused``，
    本站把它记成 ``504 upstream_unreachable``、耗时 3847ms。

    症状极具误导性：「明明 opencode 在跑、端口也对，转发就是超时」，
    而根因是**本机直连被绕进了代理** —— 排查方向会被完全带偏
    （去查 opencode 状态、查模型可用性、查超时配置，全都不是问题所在）。

    上游（``opencode.ai``）要不要走代理由 ``settings.upstream_proxy``
    **显式**控制，不依赖环境变量 —— 环境变量是全局的，会连带影响本机链路。
    """

    def test_client_has_trust_env_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 让环境变量里的代理**确实存在**，否则这个断言是空的
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
        settings = Settings(db_path=str(tmp_path / "t.db"))
        c = Container.build(settings)
        try:
            assert c.http.trust_env is False, (
                "httpx 会读 http_proxy 环境变量 —— 本机直连 opencode "
                "会被绕进代理，表现为「opencode 在跑但转发超时」"
            )
        finally:
            c.recorder.close()
            c.database.close()

    async def test_it_actually_bypasses_the_proxy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不是只查属性，而是**真发一次**并观察它有没有走代理。

        用一个必然失败的代理地址：``trust_env=True`` 时请求会经过它而报
        ``ProxyError``，``trust_env=False`` 时直连（本机无服务 ->
        ``ConnectError``）。

        **必须用 try/except 而不是 ``pytest.raises``**：httpx 的
        ``ProxyError`` 与 ``InvalidURL`` 是**兄弟**而不是子类，所以
        ``pytest.raises(httpx.ConnectError)`` 抓不到它，只会变成
        「那个异常没被抛出」的失败 —— 掩盖了真正的原因。
        """
        import httpx

        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
        settings = Settings(db_path=str(tmp_path / "t.db"))
        c = Container.build(settings)
        try:
            try:
                # **必须 await** —— 忘了会得到「coroutine 没有 status_code」，
                # 而那是个与本测试要验的东西毫无关系的 AttributeError。
                resp = await c.http.get("http://127.0.0.1:9/api/model")
            except httpx.HTTPError as exc:
                # 关键在**异常类型**而不是文本：走了代理是 ProxyError，
                # 直连失败是 ConnectError。两者都是 HTTPError 的子类，
                # 所以只 catching 基类再按类型细分。
                assert type(exc) is httpx.ConnectError, (
                    f"走了代理（{type(exc).__name__}: {exc}）—— "
                    "本机直连不该经过 http_proxy"
                )
            else:
                pytest.fail(f"不该成功：HTTP {resp.status_code}")
        finally:
            c.recorder.close()
            c.database.close()


class TestStartup:
    async def test_startup_prunes_expired_records_once(
        self, container: Container, settings: Settings
    ) -> None:
        container.usage.record(make_record(200))
        container.usage.record(make_record(0))
        await container.startup(start_pruner=False)
        assert container.usage.summary().requests == 1

    async def test_pruner_task_is_started_on_demand(self, container: Container) -> None:
        await container.startup(start_pruner=True)
        try:
            assert container._prune_task is not None
            assert container._prune_task.get_name() == "openproxy-pruner"
            assert not container._prune_task.done()
        finally:
            await asyncio.wait_for(container.shutdown(), timeout=5)

    async def test_pruner_task_is_skipped_when_asked(
        self, container: Container
    ) -> None:
        await container.startup(start_pruner=False)
        assert container._prune_task is None

    def test_interval_is_six_hours(self) -> None:
        assert PRUNE_INTERVAL_SECONDS == 6 * 3600.0


class TestPruneLoop:
    async def test_each_tick_deletes_expired_records(
        self, container: Container
    ) -> None:
        """循环要真的按拍清理，而不是「启动时清一次就再也不管了」。"""
        task = asyncio.create_task(container._prune_loop(interval=0.01))
        try:
            container.usage.record(make_record(200))
            for _ in range(200):
                if container.usage.summary().requests == 0:
                    break
                await asyncio.sleep(0.01)
            assert container.usage.summary().requests == 0
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_a_failing_tick_does_not_kill_the_loop(
        self, container: Container, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``prune`` 抛异常时循环必须继续下一拍，而不是静默死掉。

        之前这里没有任何断言，于是「``continue`` 写成 ``break``」或者
        「异常分支直接 return」都发现不了 —— 而那意味着一张站从此不再清理历史。
        """
        calls = {"n": 0}
        real_prune = UsageStore.prune

        def flaky(self: UsageStore, cutoff: int) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("disk on fire")
            return real_prune(self, cutoff)

        monkeypatch.setattr(UsageStore, "prune", flaky)
        task = asyncio.create_task(container._prune_loop(interval=0.01))
        try:
            container.usage.record(make_record(200))
            for _ in range(300):
                if calls["n"] >= 2 and container.usage.summary().requests == 0:
                    break
                await asyncio.sleep(0.01)
            assert calls["n"] >= 2, "第一次失败之后循环就死了"
            assert container.usage.summary().requests == 0, "第二拍必须真的清了"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestShutdown:
    async def test_shutdown_closes_every_resource(
        self, settings: Settings
    ) -> None:
        c = Container.build(settings, transport=standard_upstream(), tz_offset_minutes=TZ)
        await c.startup(start_pruner=True)
        recorder_thread = c.recorder
        pruner = c._prune_task
        assert pruner is not None
        # 关停必须是**立刻**完成的：``shutdown()`` 里先 cancel 再 await 那个 task。
        # 少了 cancel 也不会永久挂住（asyncio 取消外层任务时会顺带取消被 await 的
        # task），但要白等满整个超时预算 —— 6 小时的 sleep 让优雅退出多花 5 秒。
        # 所以这里既断言「真的被取消了」，也断言「没拖到超时」。
        started = time.monotonic()
        await asyncio.wait_for(c.shutdown(), timeout=5)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"关停耗时 {elapsed:.2f}s，说明没主动取消清理任务"
        assert pruner.cancelled(), "后台清理任务必须在关停时被取消"
        assert c._prune_task is None
        assert not recorder_thread.worker_alive, "写线程必须退出，否则进程不干净"
        assert c.database.open_connections == 0, "数据库连接必须全部关闭"

    async def test_shutdown_is_safe_without_a_pruner(self, container: Container) -> None:
        await container.startup(start_pruner=False)
        await asyncio.wait_for(container.shutdown(), timeout=5)
        assert container.database.open_connections == 0
