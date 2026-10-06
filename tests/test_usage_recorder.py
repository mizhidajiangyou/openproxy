"""用量写入通道：队列、批量、排空、丢弃、关闭。

这一层是「尽力而为」的统计，测试要钉死的是**它的边界**：什么时候会丢、
什么时候一定会写进去，以及 ``flush`` 是确定性的。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from openproxy.domain import TokenUsage, UsageRecord
from openproxy.service.usage_recorder import UsageRecorder
from openproxy.store import Database, UsageStore


def rec(ts: int = 1_791_028_800_000, **kw: object) -> UsageRecord:
    base: dict[str, object] = {
        "ts": ts,
        "model": "space-bunny-free",
        "path": "/v1/chat/completions",
        "stream": False,
        "status": 200,
        "latency_ms": 5,
        "usage": TokenUsage(1, 1, 0, 0, 2, True),
    }
    base.update(kw)
    return UsageRecord(**base)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[UsageStore]:
    db = Database(tmp_path / "t.db")
    db.migrate()
    yield UsageStore(db, tz_offset_minutes=480)
    db.close()


class FlakyStore(UsageStore):
    """第一次 :meth:`record` 抛异常，之后恢复正常 —— 模拟一次磁盘抖动。"""

    def __init__(self, inner: UsageStore) -> None:
        # 复用同一条底层连接，所以统计和断言看到的是同一个库
        super().__init__(inner.db)
        self._inner = inner
        self.attempts = 0

    def record(self, rec: UsageRecord) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("disk on fire")
        self._inner.record(rec)


class TestDelivery:
    def test_single_record_reaches_the_store(self, store: UsageStore) -> None:
        r = UsageRecorder(store)
        try:
            assert r.record(rec()) is True
            assert r.flush(2.0) is True
            assert store.summary().requests == 1
        finally:
            r.close()

    def test_bulk_records_all_land(self, store: UsageStore) -> None:
        r = UsageRecorder(store, batch_size=16)
        try:
            for i in range(500):
                r.record(rec(ts=1_791_028_800_000 + i))
            assert r.flush(5.0) is True
            assert store.summary().requests == 500
            assert r.written == 500
            assert r.dropped == 0
            assert r.failed == 0
        finally:
            r.close()

    def test_key_last_used_is_touched(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "k.db")
        db.migrate()
        from openproxy.store import KeyStore

        keys = KeyStore(db)
        key, _raw = keys.create("甲")
        usage = UsageStore(db, tz_offset_minutes=480)
        r = UsageRecorder(usage)
        try:
            r.record(rec(key_id=key.id, key_label="甲"), key.id)
            assert r.flush(2.0) is True
            found = keys.get(key.id)
            assert found is not None and found.last_used_at == rec().ts
        finally:
            r.close()
            db.close()

    def test_last_used_does_not_leak_across_keys_in_a_batch(
        self, tmp_path: Path
    ) -> None:
        """一批里有多张密钥时，每张的 ``last_used_at`` 只看**自己**的记录。

        整批共用一个 ``max(ts)`` 会串号：先入队的甲(1000) 被后入队的乙(2000)
        盖成 2000。单 key 的用例看不到这个 —— 那一批里只有一个 max。
        """
        db = Database(tmp_path / "k.db")
        db.migrate()
        from openproxy.store import KeyStore

        keys = KeyStore(db)
        a, _ = keys.create("甲")
        b, _ = keys.create("乙")
        usage = UsageStore(db, tz_offset_minutes=480)
        r = UsageRecorder(usage, batch_size=8, flush_interval=30.0)
        try:
            r.record(rec(ts=1_791_028_800_000, key_id=a.id, key_label="甲"), a.id)
            r.record(rec(ts=1_791_029_000_000, key_id=b.id, key_label="乙"), b.id)
            r.record(rec(ts=1_791_028_900_000, key_id=a.id, key_label="甲"), a.id)
            assert r.pending == 3, "三条必须还堵在队列里，才会落进同一批"
            assert r.flush(5.0) is True
            got_a = keys.get(a.id)
            got_b = keys.get(b.id)
            assert got_a is not None and got_b is not None
            assert got_a.last_used_at == 1_791_028_900_000, "甲取自己两条里的较大值"
            assert got_b.last_used_at == 1_791_029_000_000
        finally:
            r.close()
            db.close()

    def test_records_without_key_id_skip_the_touch(self, store: UsageStore) -> None:
        r = UsageRecorder(store)
        try:
            r.record(rec(), None)
            assert r.flush(2.0) is True
            assert store.summary().requests == 1
        finally:
            r.close()

    def test_last_used_is_the_newest_timestamp_of_the_batch(self, tmp_path: Path) -> None:
        """乱序入队（并发转发天然如此）时 ``last_used_at`` 要取**最大**的 ts。

        写死 ``min(ts)`` 会让「最近使用」永远停在那一批里最早的那次调用 ——
        单条记录的用例完全观察不到，因为一批里只有一条。
        """
        db = Database(tmp_path / "k.db")
        db.migrate()
        from openproxy.store import KeyStore

        keys = KeyStore(db)
        key, _raw = keys.create("甲")
        usage = UsageStore(db, tz_offset_minutes=480)
        r = UsageRecorder(usage, batch_size=8, flush_interval=30.0)
        try:
            # 故意乱序：一批里同时含最早与最晚的时间戳
            for ts in (1_791_028_900_000, 1_791_028_800_000, 1_791_029_000_000):
                r.record(rec(ts=ts, key_id=key.id, key_label="甲"), key.id)
            assert r.pending == 3, "三条必须还堵在队列里，才会落进同一批"
            assert r.flush(5.0) is True
            found = keys.get(key.id)
            assert found is not None
            assert found.last_used_at == 1_791_029_000_000
        finally:
            r.close()
            db.close()


class TestBackpressure:
    def test_full_queue_drops_instead_of_blocking(self, store: UsageStore) -> None:
        """转发路径绝不能因为写不进统计而卡住 —— 丢统计比阻塞中转站好。"""
        blocked = threading.Event()
        release = threading.Event()

        class SlowStore(UsageStore):
            def record(self, rec: UsageRecord) -> None:
                blocked.set()
                release.wait(5)
                super().record(rec)

        slow = SlowStore(store._db)
        r = UsageRecorder(slow, queue_size=16, batch_size=1, flush_interval=60)
        try:
            accepted = [r.record(rec(ts=1_791_028_800_000 + i)) for i in range(500)]
            blocked.wait(2)  # 写线程正在忙第一条
            assert accepted.count(False) > 0  # 有记录被丢了
            assert r.dropped == accepted.count(False)
        finally:
            release.set()
            r.close()

    def test_drop_counter_accumulates(self, store: UsageStore) -> None:
        r = UsageRecorder(store, queue_size=16, batch_size=1, flush_interval=60)
        accepted = 0
        for _ in range(200):
            accepted += int(r.record(rec()))
        r.close(timeout=3)  # 关闭会排空，所以统计口径这时才是完整的
        assert r.dropped > 0
        assert r.dropped == 200 - accepted
        # 一条记录要么落库、要么被记为丢弃，不能凭空消失
        assert r.written + r.dropped + r.failed == 200
        assert store.summary().requests == r.written


class TestRobustness:
    def test_writer_thread_survives_a_bad_batch(self, store: UsageStore) -> None:
        """一条坏数据不能带死写线程 —— 否则统计从某一刻起彻底停摆。"""
        flaky = FlakyStore(store)
        r = UsageRecorder(flaky, batch_size=4, flush_interval=60)
        try:
            for i in range(40):
                r.record(rec(ts=1_791_028_800_000 + i))
            r.close(timeout=3)
            assert r.failed == 1, "只有第 1 次调用真的失败了"
            assert r.written == 39, "其余 39 条都该落库"
            assert r.written + r.failed == 40, "written 与 failed 必须互斥且不重不漏"
            assert store.summary().requests == 39
        finally:
            r.close()

    def test_flush_on_empty_queue_is_immediate(self, store: UsageStore) -> None:
        r = UsageRecorder(store)
        try:
            started = time.monotonic()
            assert r.flush(2.0) is True
            assert time.monotonic() - started < 0.5
        finally:
            r.close()

    def test_flush_returns_false_on_timeout(self, store: UsageStore) -> None:
        """超时应报 false —— ``/maintenance/flush`` 把它当权威口径报出去。"""
        release = threading.Event()
        blocked = threading.Event()

        class StuckStore(UsageStore):
            def record(self, rec: UsageRecord) -> None:
                blocked.set()
                release.wait(5)

        stuck = StuckStore(store.db)
        r = UsageRecorder(stuck, batch_size=1, flush_interval=60)
        try:
            r.record(rec())
            blocked.wait(2)
            assert r.flush(0.2) is False, "写线程卡住时 flush 必须报超时"
        finally:
            release.set()
            r.close()


class TestLifecycle:
    def test_close_drains_the_queue(self, store: UsageStore) -> None:
        r = UsageRecorder(store, batch_size=64, flush_interval=60)
        for i in range(300):
            r.record(rec(ts=1_791_028_800_000 + i))
        r.close(timeout=5)
        assert store.summary().requests == 300
        assert r.pending == 0

    def test_record_after_close_is_refused(self, store: UsageStore) -> None:
        r = UsageRecorder(store)
        r.close()
        assert r.record(rec()) is False
        assert store.summary().requests == 0

    def test_close_stops_the_writer_thread(self, store: UsageStore) -> None:
        """回归：哨兵塞不进去（队列满）时曾直接 return，
        写线程永远看不到停止信号，会空转到进程结束。"""
        r = UsageRecorder(store, queue_size=16, batch_size=1, flush_interval=60)
        try:
            for i in range(200):
                r.record(rec(ts=1_791_028_800_000 + i))
            r.close(timeout=2.0)
            assert r.worker_alive is False, "close() 之后写线程还活着"
        finally:
            r.close()

    def test_close_accounts_for_every_submitted_record(self, store: UsageStore) -> None:
        """关停时队列塞满也不能丢记录。

        口径是**提交总数**而不是被接受的条数 —— 被拒绝的那些记在 ``dropped`` 里，
        一样要出现在等式右边。
        """
        submitted = 200
        r = UsageRecorder(store, queue_size=16, batch_size=1, flush_interval=60)
        accepted = sum(int(r.record(rec(ts=1_791_028_800_000 + i))) for i in range(submitted))
        assert accepted < submitted, "本用例需要队列真的被填满"
        r.close(timeout=3.0)
        assert r.written + r.dropped + r.failed == submitted, "每条提交必须恰好记一次"
        assert r.written == accepted, "已被接受的记录不该在关停时丢掉"
        assert r.dropped == submitted - accepted
        assert store.summary().requests == r.written

    def test_close_is_idempotent(self, store: UsageStore) -> None:
        r = UsageRecorder(store)
        r.record(rec())
        r.close()
        r.close()
        assert store.summary().requests == 1

    def test_worker_thread_is_daemon(self, store: UsageStore) -> None:
        r = UsageRecorder(store)
        try:
            assert r._worker.daemon is True
        finally:
            r.close()

    def test_pending_reflects_queue_depth(self, store: UsageStore) -> None:
        """pending 必须真的反映队列深度。

        之前写的是 ``assert r.pending >= 0`` —— 恒真，把 ``pending`` 硬编码成
        ``return 0`` 整个套件照样全绿。而 ``pending`` 是
        ``POST /api/admin/maintenance/flush`` 报出去的字段之一。
        """
        release = threading.Event()
        blocked = threading.Event()

        class SlowStore(UsageStore):
            def record(self, rec: UsageRecord) -> None:
                blocked.set()
                release.wait(5)
                super().record(rec)

        slow = SlowStore(store.db)
        r = UsageRecorder(slow, queue_size=10_000, batch_size=1, flush_interval=60)
        try:
            r.record(rec())
            blocked.wait(2)  # 写线程卡在第一条上
            for i in range(1, 40):
                r.record(rec(ts=1_791_028_800_000 + i))
            assert r.pending > 0, "写线程被卡住时队列里应当还有未处理的记录"
            assert r.pending < 40, "pending 不该把已写的也算进去"
        finally:
            release.set()
            r.close()


class TestConcurrency:
    def test_many_producers_lose_nothing_when_not_saturated(self, store: UsageStore) -> None:
        r = UsageRecorder(store, queue_size=10_000, batch_size=64, flush_interval=0.05)
        try:
            def worker(base: int) -> None:
                for i in range(100):
                    r.record(rec(ts=1_791_028_800_000 + base * 100 + i))

            threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            r.close(timeout=5)
            assert r.dropped == 0
            assert store.summary().requests == 600
        finally:
            r.close()
