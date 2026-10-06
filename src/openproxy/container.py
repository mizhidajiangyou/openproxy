"""容器：所有长生命周期对象的构造与生命周期。

一个 :class:`Container` 持有整套依赖，测试用同一个类换成临时 DB / 假上游，
所以「生产怎么拼的」和「测试怎么拼的」只有一份代码（R17：mock 只证明接线，
所以生产路径本身必须是可复用的组装函数，而不是散落在 ``create_app`` 里）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType

import httpx

from openproxy.config import Settings
from openproxy.service.auth import AuthService, QuotaService
from openproxy.service.config_service import ConfigService
from openproxy.service.dashboard import DashboardService
from openproxy.service.model_catalog import ModelCatalog
from openproxy.service.proxy import ProxyService
from openproxy.service.usage_recorder import UsageRecorder
from openproxy.store import (
    DAY_MS,
    Database,
    KeyStore,
    ProbeStore,
    UsageStore,
    day_start_ms,
    now_ms,
)

log = logging.getLogger("openproxy")

#: 定期清理的间隔。抽成常量而不是写死在循环体里有两个理由：
#: 一眼能看出「多久清一次」；二是测试可以用一个很小的间隔真的把循环**跑起来** ——
#: 之前这个循环（含它的异常分支）在整套件里一次都没执行过。
PRUNE_INTERVAL_SECONDS = 6 * 3600.0

#: 可达性探测的时刻（本地时间）。用户明确要求「每天凌晨 2 点扫一遍」。
#:
#: 为什么不是「每 24 小时跑一次」：那会让探测时刻随进程启动时间漂移，
#: 而那 9 个模型的 ``blocked`` 是要看清楚的长期状态 —— 固定时刻扫，
#: 日志与告警的时间点才可预期。
REACHABILITY_HOUR = 2
REACHABILITY_MINUTE = 0


def seconds_until_next(hour: int, minute: int, *, now: datetime | None = None) -> float:
    """距离下一个 ``hour:minute`` 还有多少秒。用于每日定时任务的对齐。

    对齐而不是「每 86400 秒」：后者的触发时刻随进程启动时间漂移，于是「每天凌晨
    2 点扫一遍」会变成「每天启动后 24 小时扫一遍」。两者在日志里看起来一样，
    但只有对齐的那个能回答「昨天凌晨那次扫的结果是什么」。

    已经过了今天的点就算到明天 —— 于是启动时正好是 3 点，会等到明天 2 点，
    不会在启动后一小时内把探测跑掉（那会撞上用户正在用的额度）。
    """
    current = now or datetime.now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


def build_timeout(settings: Settings) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.connect_timeout,
        read=settings.read_timeout,
        write=settings.read_timeout,
        pool=settings.connect_timeout,
    )


@dataclass(slots=True)
class Container:
    """依赖集合。``async with`` 保证写线程与 HTTP 连接池被干净关闭。"""

    settings: Settings
    database: Database
    keys: KeyStore
    usage: UsageStore
    config: ConfigService
    auth: AuthService
    quota: QuotaService
    catalog: ModelCatalog
    recorder: UsageRecorder
    http: httpx.AsyncClient
    proxy: ProxyService
    dashboard: DashboardService
    probes: ProbeStore
    _prune_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _probe_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        tz_offset_minutes: int | None = None,
    ) -> Container:
        """构造整套依赖。

        ``transport`` 只给测试用：换成假 transport 就能在不发真实网络请求的前提下
        跑完整条转发链路（包含 SSE 分块）。

        ``tz_offset_minutes`` 决定「今天」的边界。``None`` = 取本机当前偏移；
        测试钉死它，于是按天聚合的结果不随运行机器的时区变化。
        """
        database = Database(Path(settings.db_path))
        database.migrate()
        usage = UsageStore(database, tz_offset_minutes=tz_offset_minutes)
        keys = KeyStore(database)
        probes = ProbeStore(database)
        config = ConfigService(database, settings)
        auth = AuthService(keys, usage)
        quota = QuotaService(usage)
        catalog = ModelCatalog(store=probes)
        recorder = UsageRecorder(usage)
        # ``trust_env=False``：**不让 httpx 读 ``http_proxy`` / ``https_proxy``
        # 等环境变量**。
        #
        # 为什么必须显式关掉（实测 2026-10-05，用户踩了这个坑）：
        # 开着 ``trust_env``（httpx 的默认值）时，连本机的 opencode 也会被塞进
        # 代理 —— ``http_proxy=http://127.0.0.1:50572`` 会把
        # ``127.0.0.1:4096`` 那个请求转发给代理，代理再连不上就回
        # ``502 upstream connect failed``，本站把它记成 504
        # ``upstream_unreachable``。症状是「明明 opencode 在跑，
        # 转发就是超时」，而根因是**本机直连被绕进了代理**。
        #
        # 上游（opencode.ai）要不要走代理由 ``settings.upstream_proxy`` 显式控制，
        # 不依赖环境变量 —— 环境变量是全局的，会连带影响本机转发这条链路。
        http = httpx.AsyncClient(
            timeout=build_timeout(settings),
            transport=transport,
            trust_env=False,
        )
        proxy = ProxyService(http, config, auth, quota, catalog, recorder)
        dashboard = DashboardService(usage, catalog)
        return cls(
            settings=settings,
            database=database,
            keys=keys,
            usage=usage,
            config=config,
            auth=auth,
            quota=quota,
            catalog=catalog,
            recorder=recorder,
            http=http,
            proxy=proxy,
            dashboard=dashboard,
            probes=probes,
        )

    # ---------------------------------------------------------- 生命周期 ---

    async def startup(
        self, *, start_pruner: bool = True, start_prober: bool | None = None
    ) -> None:
        config = self.config.snapshot
        cutoff = day_start_ms(now_ms(), self.usage.tz_offset_minutes) - config.retain_days * DAY_MS
        removed = self.usage.prune(cutoff)
        if removed:
            log.info("启动清理：删除了 %d 条超过 %d 天的用量记录", removed, config.retain_days)
        if start_pruner:
            self._prune_task = asyncio.create_task(self._prune_loop(), name="openproxy-pruner")
        # ``start_prober=None`` = 跟随 settings.probe_reachability。测试要真正
        # 关掉它必须显式传 False：探测会发 10 次真实请求，而多条测试断言
        # 「调用 /v1/__health 不许碰上游」—— 一个后台探测任务就能让那条断言失真。
        want_prober = (
            self.settings.probe_reachability if start_prober is None else start_prober
        )
        if want_prober:
            self._probe_task = asyncio.create_task(
                self._reachability_loop(), name="openproxy-prober"
            )

    async def shutdown(self) -> None:
        for task in (self._prune_task, self._probe_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._prune_task = None
        self._probe_task = None
        await self.http.aclose()
        self.recorder.close()
        # **顺序要紧**：``asyncio.to_thread`` 的池线程各自持有 ``threading.local``
        # 连接。先把池停掉，那些线程退出、连接随之回收，然后才能关库。反过来做，
        # 池线程会在 :meth:`Database.close` 之后又按需新建一条连接，而那条再没人负责
        # —— 在 Python 3.14 上直接表现为 ``ResourceWarning`` 测试失败，且**归属哪个
        # 用例随机漂移**（GC 在哪一刻回收就记到哪一刻，实测连跑三次漂了两次）。
        await self._shutdown_executor()
        self.database.close()

    @staticmethod
    async def _shutdown_executor() -> None:
        """停掉当前事件循环的 ``to_thread`` 默认线程池。

        为什么必须显式停：那个池是**进程级共享**的，线程活到事件循环结束。
        「关库」与「关线程池」不对齐的话，池线程会在库关掉之后继续新建连接，
        而那些连接已经不在 :attr:`Database._all_conns` 的登记里 —— 谁都关不掉。

        用 ``shutdown_default_executor`` 而不是自建线程池：它是 asyncio 官方为这件事
        提供的入口，且 ``shutdown()`` 本来就是 async，直接 await 即可，
        不必自己维护一个 executor 的生命周期。
        """
        loop = asyncio.get_running_loop()
        with contextlib.suppress(RuntimeError):
            # 没有实际用过 to_thread 时它是 None（部分版本），suppress 掉即可
            await loop.shutdown_default_executor()

    async def _prune_loop(self, interval: float = PRUNE_INTERVAL_SECONDS) -> None:
        """每 6 小时清理一次过期用量。进程内跑，不引入第二个调度器。

        ``interval`` 是带默认值的参数而不是读全局常量：测试要用毫秒级的间隔真的
        驱动这个循环，否则「每 6 小时清一次」里的**每 6 小时**从来没人验证过。
        """
        while True:
            await asyncio.sleep(interval)
            config = self.config.snapshot
            cutoff = day_start_ms(
                now_ms(), self.usage.tz_offset_minutes
            ) - config.retain_days * DAY_MS
            try:
                removed = await asyncio.to_thread(self.usage.prune, cutoff)
            except Exception as exc:
                log.warning("定期清理用量失败: %r", exc)
                continue
            if removed:
                log.info("定期清理：删除了 %d 条超过 %d 天的记录", removed, config.retain_days)

    async def _reachability_loop(
        self,
        *,
        hour: int = REACHABILITY_HOUR,
        minute: int = REACHABILITY_MINUTE,
        run_on_start: bool = True,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """每天凌晨 ``hour:minute`` 扫一遍「站外能不能调通」，结果落库。

        启动时也跑一次，但**只在结果已过期（超过一天）时**才真发请求 ——
        否则「每天 2 点」会变成「每次重启都发 10 个请求」。

        ``sleep`` 是带默认值的注入点，与 :meth:`_prune_loop` 的 ``interval``
        同一个理由：``seconds_until_next`` 返回的是**几小时**，不注入的话
        「循环真的自己跑了一拍」这条断言永远无法在测试里成立。

        整轮失败（网络不通、上游改路由）不能杀掉循环：这是长期观测任务，
        一次失败不代表明天不该再试。
        """
        if run_on_start and self.catalog.is_stale(now_ms=now_ms()):
            await self._run_reachability()
        while True:
            await sleep(seconds_until_next(hour, minute))
            await self._run_reachability()

    async def _run_reachability(self) -> None:
        try:
            result = await self.catalog.probe(
                self.http,
                self.config.snapshot.upstream_base,
                reachability=True,
            )
        except Exception as exc:
            log.warning("站外可达性探测失败: %r", exc)
            return
        if not result.ok:
            log.warning("站外可达性探测：上游不可达（%s）", result.detail)
            return
        reachability = result.reachability or {}
        reachable = sum(1 for r in reachability.values() if r.status == "ok")
        log.info(
            "站外可达性探测完成：%d/%d 个模型站外可调", reachable, len(reachability)
        )

    async def __aenter__(self) -> Container:
        await self.startup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.shutdown()

    # -------------------------------------------------------------- 便捷 ---

    def public_config(self) -> dict[str, object]:
        return self.config.snapshot.public_dict()

    async def prune_now(self) -> int:
        config = self.config.snapshot
        cutoff = day_start_ms(now_ms(), self.usage.tz_offset_minutes) - config.retain_days * DAY_MS
        return await asyncio.to_thread(self.usage.prune, cutoff)
