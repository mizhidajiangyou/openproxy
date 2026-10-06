"""用量落库的写入通道。

为什么不是一个 ``await store.record(...)``
------------------------------------------
记账发生在流式生成器的 ``finally`` 里 —— 那条路径随时可能正在处理
``GeneratorExit`` 或 ``asyncio.CancelledError``（客户端断开），在那里 ``await``
会把「关流」和「等 IO」耦合在一起，而 sqlite 写入只有零点几毫秒，把它放到事件
循环里既简单又无害。

但零星同步写会在高 QPS 时阻塞事件循环，而丢一点统计数据比阻塞整个中转站好。
所以折中：**有界队列 + 单个 daemon 写线程 + 批量插入**。

计数口径（硬约定）
------------------
``written + dropped + failed == record() 被接受的总数``，**每条只落一次账**。

曾经写成「整批成功计数 + 整批失败计数」：``UsageStore.record()`` 自带
``BEGIN IMMEDIATE``，所以一批 N 条其实是 N 个独立事务。批中第 3 条失败时，
前 2 条已经提交了，却把 N 条同时记进 ``written`` 和 ``failed`` —— 实测
6 条会报出 ``written=6 failed=6``，而库里只有 2 行。``/maintenance/flush``
把这些数字当成权威口径报出去，所以它必须是真值。

代价与边界（必须知道，否则会误读数据）
----------------------------------------
* 队列满时**丢弃并计数**（``dropped``），绝不阻塞转发路径 —— 中转站因为写不进
  统计而卡死，比统计丢几条更糟。
* 进程被 ``SIGKILL`` 时队列里的记录会丢。优雅退出会 :meth:`close` 排空，
  甚至在队列塞满时也会同步把剩下的写完。因此这是「尽力而为」的统计，不是账务系统。
* :meth:`flush` 是同步的，测试和优雅退出都用它确定性地排空。
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from dataclasses import dataclass

from openproxy.domain import UsageRecord
from openproxy.store import UsageStore

log = logging.getLogger("openproxy.recorder")

_SENTINEL = object()


@dataclass(frozen=True, slots=True)
class _Job:
    record: UsageRecord
    key_id: str | None


class UsageRecorder:
    def __init__(
        self,
        store: UsageStore,
        *,
        queue_size: int = 10_000,
        batch_size: int = 64,
        flush_interval: float = 0.2,
    ) -> None:
        self._store = store
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max(16, queue_size))
        self._batch_size = max(1, batch_size)
        self._flush_interval = max(0.01, flush_interval)
        self._stopped = False
        self.dropped = 0
        self.failed = 0
        self._written = 0
        self._idle = threading.Event()
        self._idle.set()
        self._worker = threading.Thread(
            target=self._run, name="openproxy-usage-writer", daemon=True
        )
        self._worker.start()

    # --------------------------------------------------------- 生产者 ---

    def record(self, rec: UsageRecord, key_id: str | None = None) -> bool:
        """入队一条。返回是否入队成功。**不阻塞**。"""
        if self._stopped:
            return False
        try:
            self._queue.put_nowait(_Job(record=rec, key_id=key_id))
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                log.warning("用量队列已满，已丢弃 %d 条统计记录", self.dropped)
            return False
        self._idle.clear()
        return True

    # --------------------------------------------------------- 消费者 ---

    def _run(self) -> None:
        while True:
            batch: list[_Job] = []
            try:
                first = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                continue
            if first is _SENTINEL:
                self._drain_into(batch)
                self._write(batch)
                return
            if isinstance(first, _Job):
                batch.append(first)
            while len(batch) < self._batch_size:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is _SENTINEL:
                    self._write(batch)
                    return
                if isinstance(item, _Job):
                    batch.append(item)
            self._write(batch)

    def _drain_into(self, batch: list[_Job]) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _Job):
                batch.append(item)

    def _write(self, batch: list[_Job]) -> None:
        """逐条写、逐条计数。

        不按批记成功/失败的原因见模块 docstring：``UsageStore.record()`` 自己是
        一个事务，批里第 k 条失败时前 k-1 条已经落库了。
        """
        try:
            for job in batch:
                self._write_one(job)
            # **按 key 分别**取最大值。整批共用一个 max 会串号：一批里先入队的
            # 甲(ts=1000) 与后入队的乙(ts=2000) 会把甲的 last_used_at 写成 2000 ——
            # 密钥页的「最近使用」显示成同一批里别的密钥的完成时间。
            newest: dict[str, int] = {}
            for job in batch:
                if not job.key_id:
                    continue
                previous = newest.get(job.key_id)
                newest[job.key_id] = (
                    job.record.ts if previous is None else max(previous, job.record.ts)
                )
            for key_id, ts in newest.items():
                self._store.touch_key(key_id, ts)
        finally:
            self._mark_idle()

    def _write_one(self, job: _Job) -> bool:
        """写一条。成功计入 ``written``，失败计入 ``failed``，**互斥**。"""
        try:
            self._store.record(job.record)
        except Exception as exc:
            self.failed += 1
            log.warning("写入用量失败: %r", exc)
            return False
        self._written += 1
        return True

    def _mark_idle(self) -> None:
        if self._queue.empty():
            self._idle.set()

    # ------------------------------------------------------------ 控制 ---

    @property
    def written(self) -> int:
        return self._written

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def worker_alive(self) -> bool:
        """写线程是否还在跑。关停路径上有专门的断言，见 :meth:`close`。"""
        return self._worker.is_alive()

    def flush(self, timeout: float = 5.0) -> bool:
        """等队列排空。返回是否在超时前排空。"""
        if not self._idle.wait(timeout):
            return False
        return self._queue.empty()

    def close(self, timeout: float = 5.0) -> None:
        """停止写线程并排空队列。可重复调用。

        哨兵塞不进去（队列满）时**不能就这么返回**：那样写线程永远看不到停止信号，
        会在整个进程生命周期里空转，而那批已接受的记录既没写、也没被算进任何计数 ——
        ``/maintenance/flush`` 于是低报。所以那条分支里由调用线程同步把剩下的写完，
        再塞哨兵，并保证 ``join`` 一定执行。
        """
        if self._stopped:
            self.flush(timeout)
            return
        self._stopped = True
        deadline = time.monotonic() + max(0.1, timeout)
        try:
            self._queue.put(_SENTINEL, timeout=max(0.05, deadline - time.monotonic()))
        except queue.Full:
            log.warning("关闭时用量队列已满，由调用线程同步写完剩余 %d 条", self._queue.qsize())
            self._write(self._drain_all())
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(_SENTINEL)
        self._worker.join(timeout=max(0.05, deadline - time.monotonic()))
        self._idle.set()

    def _drain_all(self) -> list[_Job]:
        out: list[_Job] = []
        self._drain_into(out)
        return out
