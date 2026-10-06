"""用量仓储：写入、过滤分页、聚合、时区边界、并发、清理。"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from openproxy.domain import (
    ErrorKind,
    TokenUsage,
    UsageFilter,
    UsageRecord,
)
from openproxy.store import (
    DAY_MS,
    SCHEMA_VERSION,
    Database,
    KeyStore,
    UsageStore,
    day_label,
    day_start_ms,
    now_ms,
    tz_modifier_for,
)

TZ = 480  # UTC+8，钉死以让「今天」的边界确定

# 2026-10-03 12:00:00Z 的 epoch 毫秒。用常量而不是 now()，断言才是确定值。
TS_2026_10_03_NOON_UTC = 1_791_028_800_000


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "t.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def store(db: Database) -> UsageStore:
    return UsageStore(db, tz_offset_minutes=TZ)


def make(ts: int, **kw: object) -> UsageRecord:
    base: dict[str, object] = {
        "ts": ts,
        "model": "space-bunny-free",
        "path": "/v1/chat/completions",
        "stream": False,
        "status": 200,
        "latency_ms": 100,
        "usage": TokenUsage(10, 5, 2, 1, 15, True),
    }
    base.update(kw)
    return UsageRecord(**base)  # type: ignore[arg-type]


class TestTimeHelpers:
    @pytest.mark.parametrize("offset", [0, 480, -480, 330, 840, -840])
    def test_modifier_round_trips(self, offset: int) -> None:
        assert tz_modifier_for(offset) == f"{offset:+d} minutes"

    def test_modifier_rejects_absurd_offset(self) -> None:
        with pytest.raises(ValueError, match="时区偏移"):
            tz_modifier_for(15 * 60)

    def test_day_start_is_local_midnight(self) -> None:
        """UTC+8 的本地午夜在 UTC 里是前一天 16:00，因此不能断言「整 UTC 日切点」。"""
        start = day_start_ms(TS_2026_10_03_NOON_UTC, TZ)
        assert day_label(start, TZ) == "2026-10-03"  # UTC 正午 = UTC+8 当天 20:00
        assert day_label(start - 1, TZ) == "2026-10-02"
        assert day_label(start + DAY_MS - 1, TZ) == "2026-10-03"
        assert start == start - start % 1_000  # 落在整秒上，SQLite date() 才认得
        assert (start // 3_600_000) % 24 == 16  # UTC 16:00 == UTC+8 的 00:00

    def test_day_start_utc(self) -> None:
        start = day_start_ms(TS_2026_10_03_NOON_UTC, 0)
        assert day_label(start, 0) == "2026-10-03"
        assert start % DAY_MS == 0

    def test_records_just_before_and_after_midnight_land_on_different_days(self) -> None:
        today = day_start_ms(now_ms(), TZ)
        assert day_label(today - 1, TZ) != day_label(today, TZ)
        assert day_label(today, TZ) == day_label(today + DAY_MS - 1, TZ)


class TestRecordAndRead:
    def test_round_trip_preserves_every_field(self, store: UsageStore) -> None:
        original = make(
            now_ms(),
            key_id="k1",
            key_label="甲",
            anonymous=False,
            bytes_in=10,
            bytes_out=20,
            error_kind=ErrorKind.UPSTREAM_TIMEOUT,
            client_ip="127.0.0.1",
            stream=True,
            status=504,
        )
        store.record(original)
        got = store.list(UsageFilter(page_size=1)).items[0]
        assert got.key_id == "k1"
        assert got.key_label == "甲"
        assert got.anonymous is False
        assert got.bytes_in == 10
        assert got.bytes_out == 20
        assert got.error_kind is ErrorKind.UPSTREAM_TIMEOUT
        assert got.client_ip == "127.0.0.1"
        assert got.stream is True
        assert got.status == 504
        assert got.usage == original.usage

    def test_unknown_usage_round_trips_as_not_known(self, store: UsageStore) -> None:
        store.record(make(now_ms(), usage=TokenUsage.unknown()))
        assert store.list(UsageFilter()).items[0].usage.known is False

    def test_unknown_error_kind_degrades_to_internal(
        self, db: Database, store: UsageStore
    ) -> None:
        """库里出现未知错误类别（比如老版本写过）不能让读取抛异常。"""
        store.record(make(now_ms(), error_kind=ErrorKind.INTERNAL))
        with db.write() as conn:
            conn.execute("UPDATE usage_records SET error_kind = 'from_the_future'")
        assert store.list(UsageFilter()).items[0].error_kind is ErrorKind.INTERNAL

    def test_empty_store_returns_empty_page_not_error(self, store: UsageStore) -> None:
        page = store.list(UsageFilter())
        assert page.items == ()
        assert page.total == 0
        assert page.pages == 1

    def test_summary_of_empty_store_is_all_zero(self, store: UsageStore) -> None:
        s = store.summary()
        assert (s.requests, s.errors, s.total_tokens, s.avg_latency_ms) == (0, 0, 0, 0)


class TestListFiltering:
    @pytest.fixture
    def populated(self, store: UsageStore) -> UsageStore:
        base = day_start_ms(now_ms(), TZ)
        for i in range(30):
            store.record(
                make(
                    base + i * 60_000,
                    model="space-bunny-free" if i % 2 == 0 else "ling-3.1-flash-free",
                    status=200 if i % 5 else 500,
                    error_kind=ErrorKind.NONE if i % 5 else ErrorKind.UPSTREAM_STATUS,
                    key_id="k1" if i % 3 else None,
                    key_label="甲" if i % 3 else "",
                    anonymous=i % 3 == 0,
                )
            )
        return store

    def test_newest_first(self, populated: UsageStore) -> None:
        items = populated.list(UsageFilter(page_size=5)).items
        assert [i.ts for i in items] == sorted((i.ts for i in items), reverse=True)

    def test_model_filter(self, populated: UsageStore) -> None:
        page = populated.list(UsageFilter(model="ling-3.1-flash-free"))
        assert page.total == 15
        assert all(i.model == "ling-3.1-flash-free" for i in page.items)

    def test_status_filter_ok_and_error(self, populated: UsageStore) -> None:
        assert populated.list(UsageFilter(status="ok")).total == 24
        assert populated.list(UsageFilter(status="error")).total == 6

    def test_unknown_status_value_is_ignored_not_error(self, populated: UsageStore) -> None:
        assert populated.list(UsageFilter(status="weird")).total == 30

    def test_anonymous_only(self, populated: UsageStore) -> None:
        assert populated.list(UsageFilter(anonymous_only=True)).total == 10

    def test_key_id_filter(self, populated: UsageStore) -> None:
        assert populated.list(UsageFilter(key_id="k1")).total == 20

    def test_range_filter_is_half_open(self, store: UsageStore) -> None:
        now = now_ms()
        store.record(make(now - 10_000))
        store.record(make(now))
        assert store.list(UsageFilter(since=now)).total == 1
        assert store.list(UsageFilter(until=now)).total == 1
        assert store.list(UsageFilter(since=now, until=now + 1)).total == 1

    @pytest.mark.parametrize(
        ("term", "expected"), [("space", 15), ("ling", 15), ("(未知)", 0), ("nomatch", 0)]
    )
    def test_search(self, populated: UsageStore, term: str, expected: int) -> None:
        assert populated.list(UsageFilter(search=term)).total == expected

    def test_search_escapes_like_wildcards(self, store: UsageStore) -> None:
        """搜索串里的 ``%`` 必须被转义，否则用户搜 "50%" 会匹配到所有行。"""
        now = now_ms()
        store.record(make(now, model="m50x"))
        store.record(make(now - 1, model="other"))
        assert store.list(UsageFilter(search="50%")).total == 0
        assert store.list(UsageFilter(search="m50")).total == 1

    def test_search_escapes_underscore(self, store: UsageStore) -> None:
        now = now_ms()
        store.record(make(now, model="a_b"))
        store.record(make(now - 1, model="axb"))
        assert store.list(UsageFilter(search="a_b")).total == 1

    def test_pagination_is_stable_and_non_overlapping(self, populated: UsageStore) -> None:
        first = populated.list(UsageFilter(page=1, page_size=10))
        second = populated.list(UsageFilter(page=2, page_size=10))
        assert first.total == second.total == 30
        assert len(first.items) == 10
        ids_first = {i.ts for i in first.items}
        ids_second = {i.ts for i in second.items}
        assert not (ids_first & ids_second)

    def test_page_size_is_clamped(self, populated: UsageStore) -> None:
        assert populated.list(UsageFilter(page=0, page_size=0)).page_size == 1
        assert populated.list(UsageFilter(page=-3, page_size=9999)).page_size == 200

    def test_page_count_rounds_up(self, store: UsageStore) -> None:
        for i in range(7):
            store.record(make(now_ms() + i))
        assert store.list(UsageFilter(page_size=3)).pages == 3


class TestAggregations:
    def test_summary_counts_errors_as_ge_400(self, store: UsageStore) -> None:
        now = now_ms()
        store.record(make(now, status=200))
        store.record(make(now - 1, status=399))
        store.record(make(now - 2, status=400))
        store.record(make(now - 3, status=502))
        s = store.summary()
        assert s.requests == 4
        assert s.errors == 2

    def test_summary_counts_unknown_usage_separately(self, store: UsageStore) -> None:
        now = now_ms()
        store.record(make(now))
        store.record(make(now - 1, usage=TokenUsage.unknown()))
        s = store.summary()
        assert s.unknown_usage == 1
        assert s.requests == 2

    def test_summary_avg_latency_rounds(self, store: UsageStore) -> None:
        now = now_ms()
        for offset, latency in enumerate([100, 200, 333]):
            store.record(make(now - offset, latency_ms=latency))
        assert store.summary().avg_latency_ms == 211  # 633/3 = 211

    def test_trend_fills_missing_days_with_zeros(self, store: UsageStore) -> None:
        """days=5 覆盖 base-4d..base。中间空着的日子必须补 0，而不是在图上画成断线。"""
        base = day_start_ms(now_ms(), TZ)
        store.record(make(base + 3600_000))               # 今天
        store.record(make(base - 2 * DAY_MS + 3600_000))  # 前天
        points = store.trend(days=5)
        assert len(points) == 5
        assert [p.requests for p in points] == [0, 0, 1, 0, 1]
        assert points[0].bucket == day_label(base - 4 * DAY_MS, TZ)
        assert points[-1].bucket == day_label(base, TZ)

    def test_trend_clamped_to_one_year(self, store: UsageStore) -> None:
        assert len(store.trend(days=0)) == 1
        assert len(store.trend(days=10_000)) == 365

    def test_trend_excludes_tomorrow(self, store: UsageStore) -> None:
        """区间是 ``[first_day, last_day + 1day)``：明天**整整 00:00** 的那一刻
        就已经在区间外了。之前这里探的是 ``+1day + 1000``，差了一整天 ——
        把上界从 ``+DAY_MS`` 改成 ``+DAY_MS + 1`` 全套件照样绿。"""
        today = day_start_ms(now_ms(), TZ)
        store.record(make(today + DAY_MS))
        store.record(make(today + DAY_MS + 1000))
        store.record(make(today + DAY_MS - 1))
        points = store.trend(days=1)
        assert points[0].requests == 1, "明天 00:00 整点必须排除，今天 23:59:59.999 必须包含"

    def test_by_model_sorted_by_tokens_desc(self, store: UsageStore) -> None:
        now = now_ms()
        store.record(make(now, model="small", usage=TokenUsage(1, 1, 0, 0, 2, True)))
        store.record(make(now - 1, model="big", usage=TokenUsage(100, 100, 0, 0, 200, True)))
        assert [m.model for m in store.by_model()] == ["big", "small"]

    def test_by_model_groups_anonymous_under_one_bucket(self, store: UsageStore) -> None:
        now = now_ms()
        store.record(make(now, key_id=None, key_label="", anonymous=True))
        store.record(make(now - 1, key_id=None, key_label="", anonymous=True))
        rows = store.by_key()
        assert len(rows) == 1
        assert rows[0].label == "未署名"
        assert rows[0].anonymous is True

    def test_by_key_snapshots_labels(self, store: UsageStore) -> None:
        """密钥改名后，历史记录必须仍显示当时的名称（label 是快照，不是 join）。"""
        now = now_ms()
        store.record(make(now, key_id="k1", key_label="旧名", anonymous=False))
        rows = store.by_key()
        assert rows[0].label == "旧名"

    def test_error_kinds_only_lists_failures(self, store: UsageStore) -> None:
        now = now_ms()
        store.record(make(now))
        store.record(make(now - 1, error_kind=ErrorKind.UPSTREAM_TIMEOUT))
        store.record(make(now - 2, error_kind=ErrorKind.UPSTREAM_TIMEOUT))
        assert store.error_kinds() == {"upstream_timeout": 2}

    def test_live_rate_window(self, store: UsageStore) -> None:
        now = 1_000_000_000_000
        store.record(make(now - 30_000, usage=TokenUsage(1, 1, 0, 0, 2, True)))
        store.record(make(now - 90_000, usage=TokenUsage(1, 1, 0, 0, 2, True)))
        assert store.live_rate(60_000, now=now) == (1, 2)

    def test_distinct_models_sorted(self, store: UsageStore) -> None:
        now = now_ms()
        store.record(make(now, model="b"))
        store.record(make(now - 1, model="a"))
        store.record(make(now - 2, model="b"))
        assert store.distinct_models() == ("a", "b")


class TestStatusTrend:
    """``status_trend`` 是渠道页主图的数据源（``channel.js`` 直接吃 ``p.ok``/``p.error``）。

    之前只断言了「返回 7 个点」，于是把两个 ``CASE WHEN`` 对调这种变异可以
    整套件通过、而图上的绿柱与红柱整体互换。"""

    def test_ok_and_error_are_not_swapped(self, store: UsageStore) -> None:
        base = day_start_ms(now_ms(), TZ)
        store.record(make(base + 1000, status=200))
        store.record(make(base + 2000, status=201))
        store.record(make(base + 3000, status=400, error_kind=ErrorKind.BAD_REQUEST))
        store.record(make(base + 4000, status=502, error_kind=ErrorKind.UPSTREAM_STATUS))
        point = store.status_trend(days=1)[0]
        assert (point.ok, point.error) == (2, 2)

    def test_boundary_status_lands_on_the_right_side(self, store: UsageStore) -> None:
        """399 算成功、400 算失败 —— 与 ``summary``、``list(status=…)`` 同一把尺子。"""
        base = day_start_ms(now_ms(), TZ)
        store.record(make(base + 1000, status=399))
        store.record(make(base + 2000, status=400))
        point = store.status_trend(days=1)[0]
        assert (point.ok, point.error) == (1, 1)
        assert store.summary().errors == 1

    def test_status_trend_fills_missing_days_with_zeroes(self, store: UsageStore) -> None:
        base = day_start_ms(now_ms(), TZ)
        store.record(make(base + 1000, status=200))
        store.record(make(base - 2 * DAY_MS + 1000, status=500, error_kind=ErrorKind.INTERNAL))
        points = store.status_trend(days=4)
        assert [(p.ok, p.error) for p in points] == [(0, 0), (0, 1), (0, 0), (1, 0)]


class TestOrderingAndLabelling:
    """三处 ``ORDER BY`` 与两处标签/搜索分支，之前都没有断言。"""

    def test_by_model_ties_break_on_requests(self, store: UsageStore) -> None:
        """同样 token 数时多的请求排前面 —— 去掉次键会把顺序交给 SQLite 自己定。"""
        now = now_ms()
        same = TokenUsage(10, 10, 0, 0, 20, True)
        store.record(make(now, model="many", usage=same))
        store.record(make(now - 1, model="few", usage=same))
        store.record(make(now - 2, model="few", usage=same))
        assert [m.model for m in store.by_model()] == ["few", "many"]

    def test_by_key_sorted_by_tokens_then_requests(self, store: UsageStore) -> None:
        """两个排序键必须**方向相反**才能钉住它们的先后：kA 的 token 少但请求多。

        如果两个键指向同一个赢家（token 多的那个请求也多），把 ``ORDER BY`` 的两项
        对调也观察不到任何变化。
        """
        now = now_ms()
        pair = TokenUsage(1, 1, 0, 0, 2, True)
        store.record(make(now, key_id="kA", usage=TokenUsage(5, 5, 0, 0, 10, True)))
        store.record(make(now - 1, key_id="kB", usage=pair))
        store.record(make(now - 2, key_id="kB", usage=pair))
        rows = store.by_key()
        assert [(k.key_id, k.requests, k.total_tokens) for k in rows] == [("kA", 1, 10), ("kB", 2, 4)]

    def test_error_kinds_sorted_by_count_desc(self, store: UsageStore) -> None:
        now = now_ms()
        for i in range(3):
            store.record(make(now - i, error_kind=ErrorKind.UPSTREAM_TIMEOUT))
        for i in range(2):
            store.record(make(now - 10 - i, error_kind=ErrorKind.BAD_REQUEST))
        store.record(make(now - 20, error_kind=ErrorKind.INTERNAL))
        assert list(store.error_kinds()) == [
            "upstream_timeout",
            "bad_request",
            "internal",
        ]

    def test_search_matches_key_label(self, store: UsageStore) -> None:
        """UI 上明写「关键词只匹配模型名、路径、错误类别与**密钥名**」。"""
        now = now_ms()
        store.record(make(now, key_id="k1", key_label="ChatBox", model="space-bunny-free"))
        store.record(make(now - 1, key_id="k2", key_label="命令行", model="space-bunny-free"))
        hits = store.list(UsageFilter(page=1, page_size=20, search="ChatBox")).items
        assert [r.key_id for r in hits] == ["k1"]

    def test_key_bucket_counts_as_anonymous_if_any_record_was(self, store: UsageStore) -> None:
        """``by_key`` 的 ``MAX(anonymous)``：桶里只要**有一条**匿名记录就算未署名。

        密钥停用期间产生的匿名调用会落进同一个桶，``MAX`` 让这个桶仍然被算进
        「未署名」那一栏（只统计署名密钥会少算）。换成 ``MIN`` 就反过来了。
        """
        now = now_ms()
        store.record(make(now - 3, key_id="k1", key_label="甲", anonymous=True))
        store.record(make(now - 2, key_id="k1", key_label="甲", anonymous=True))
        store.record(make(now - 1, key_id="k1", key_label="甲", anonymous=False))
        rows = store.by_key()
        assert len(rows) == 1
        assert rows[0].anonymous is True
        assert rows[0].requests == 3

    def test_fully_named_bucket_is_not_anonymous(self, store: UsageStore) -> None:
        """对照组：整桶都署名时必须是 False（否则上一条就成了恒真断言）。"""
        now = now_ms()
        store.record(make(now, key_id="k1", key_label="甲", anonymous=False))
        store.record(make(now - 1, key_id="k1", key_label="甲", anonymous=False))
        rows = store.by_key()
        assert rows[0].anonymous is False
        assert rows[0].requests == 2


    def test_purely_anonymous_bucket_is_anonymous(self, store: UsageStore) -> None:
        """另一个对照组：整桶都匿名时是 True。"""
        now = now_ms()
        store.record(make(now, key_id=None, key_label="", anonymous=True))
        store.record(make(now - 1, key_id=None, key_label="", anonymous=True))
        assert store.by_key()[0].anonymous is True

    def test_anonymous_bucket_label_comes_from_the_lookup_not_the_fallback(
        self, store: UsageStore
    ) -> None:
        """匿名桶的名字必须真的由名称查询给出，不能靠 ``or ANONYMOUS_LABEL`` 兜底。

        聚合那侧匿名桶的键是 ``COALESCE(key_id,'')=''``，而名称查询那侧若原样返回
        ``NULL``，``str(None)`` 会变成字符串 ``'None'``，查字典时永远命中不了 ——
        名字只是靠下游的 ``or ANONYMOUS_LABEL`` 兜底才碰巧正确。所以这里给匿名
        记录一个**别的**标签，兜底就遮不住这个洞了。
        """
        now = now_ms()
        store.record(make(now, key_id=None, key_label="自定义标签", anonymous=True))
        rows = store.by_key()
        assert rows[0].key_id == ""
        assert rows[0].label == "自定义标签"

class TestRetention:
    def test_prune_removes_only_old(self, store: UsageStore) -> None:
        now = now_ms()
        store.record(make(now - 10 * DAY_MS))
        store.record(make(now))
        assert store.prune(now - 5 * DAY_MS) == 1
        assert store.summary().requests == 1

    def test_prune_of_empty_store(self, store: UsageStore) -> None:
        assert store.prune(now_ms()) == 0

    def test_touch_key_updates_timestamp(self, db: Database, store: UsageStore) -> None:
        keys = KeyStore(db)
        key, _ = keys.create("甲")
        assert key.last_used_at is None
        store.touch_key(key.id, 12345)
        assert (keys.get(key.id) or key).last_used_at == 12345


class TestConcurrency:
    def test_parallel_writes_lose_nothing(self, store: UsageStore) -> None:
        """R05：跨线程计数不能丢。8 线程 × 60 条 = 480 条，一条都不能少。"""
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(60):
                    store.record(make(now_ms()))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert store.summary().requests == 480

    def test_writes_and_reads_interleave(self, store: UsageStore) -> None:
        """WAL 下读不应被写阻塞到失败；混跑 200 次不能抛 OperationalError。"""
        stop = threading.Event()
        failures: list[BaseException] = []

        def reader() -> None:
            try:
                while not stop.is_set():
                    store.summary()
            except BaseException as exc:
                failures.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(3)]
        for r in readers:
            r.start()
        try:
            for i in range(200):
                store.record(make(now_ms() + i))
        finally:
            stop.set()
            for r in readers:
                r.join()
        assert failures == []


class TestDatabase:
    def test_migrate_is_idempotent(self, db: Database) -> None:
        assert db.migrate() == db.migrate()

    def test_migrate_twice_keeps_data_and_runs_ddl_once(self, db: Database) -> None:
        """``migrate()`` 幂等的**实质**，不是常量相等。

        只比较 ``SCHEMA_VERSION`` 的话，把 ``for index in range(...)`` 改成无条件
        跑一遍 DDL 也照样绿 —— 而那会在已有数据的库上把 ``CREATE TABLE`` 撞成
        "table already exists"。所以断言两件事：数据还在，且版本表里只有一行。
        """
        store = UsageStore(db, tz_offset_minutes=TZ)
        store.record(make(now_ms(), model="keep-me"))
        assert db.migrate() == db.migrate() == SCHEMA_VERSION
        assert store.summary().requests == 1, "重复迁移不能把已有数据弄丢"
        rows = db.connection.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()
        assert rows["c"] == SCHEMA_VERSION, "版本表只能追加一次，否则迁移会重跑"

    def test_connection_is_in_wal_mode(self, db: Database) -> None:
        """WAL 是 ``db.py`` 文档里「读不阻塞写」的前提，去掉它整套件原本全绿。"""
        mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"

    def test_write_transaction_starts_immediate(self, db: Database) -> None:
        """写事务必须是 ``BEGIN IMMEDIATE``。

        用 trace 回调看真正发出去的语句，而不是去读源码字符串 —— 后者只是在
        断言「代码里有这几个字」，改个缩进或换种写法就假通过。
        ``BEGIN DEFERRED`` 会把锁推迟到第一条写语句才拿，读-改-写就可能交错。
        """
        seen: list[str] = []
        db.connection.set_trace_callback(seen.append)
        try:
            with db.write() as conn:
                conn.execute("INSERT INTO kv (key, value) VALUES ('t', 'v')")
        finally:
            db.connection.set_trace_callback(None)
        assert any(s.strip().upper().startswith("BEGIN IMMEDIATE") for s in seen), seen

    def test_kv_round_trip_and_overwrite(self, db: Database) -> None:
        assert db.kv_get("absent") is None
        db.kv_set("k", "v1")
        db.kv_set("k", "v2")
        assert db.kv_get("k") == "v2"

    def test_separate_thread_gets_its_own_connection(self, db: Database) -> None:
        seen: list[int] = []

        def worker() -> None:
            seen.append(id(db.connection))

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert seen and seen[0] != id(db.connection)

    def test_close_closes_the_calling_threads_connection(self, db: Database) -> None:
        conn = db.connection
        assert db.open_connections == 1
        db.close()
        assert db.open_connections == 0
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")

    def test_other_threads_connections_are_reclaimed_generationally(
        self, db: Database
    ) -> None:
        """别的线程的连接只能由它自己关（sqlite3 拒绝跨线程 close）。

        契约：``close()`` 之后，那条线程下次取连接时会先关掉旧的那条、再拿一条
        新的。**探针必须在属主线程里跑** —— 跨线程访问会先撞上
        ``check_same_thread``，报出来的错是「跨线程」而不是「已关闭」，等于没测。
        """
        report: dict[str, object] = {}
        ready = threading.Barrier(2, timeout=10)
        resumed = threading.Event()

        def worker() -> None:
            stale = db.connection
            report["stale"] = stale
            ready.wait()             # 主线程会在这里 close()
            resumed.wait(10)         # 等主线程 close 完
            fresh = db.connection
            report["fresh"] = fresh
            try:
                stale.execute("SELECT 1").fetchone()
                report["stale_closed"] = False
            except sqlite3.ProgrammingError as exc:
                report["stale_closed"] = "closed" in str(exc)

        t = threading.Thread(target=worker)
        t.start()
        ready.wait()
        db.close()
        assert db.open_connections == 0, "close() 之后登记表应当清空"

        resumed.set()
        t.join(10)

        stale = report["stale"]
        fresh = report["fresh"]
        assert fresh is not stale, "close() 之后应当换一条连接"
        assert report["stale_closed"] is True, "上一代连接没有被关掉"
        assert db.open_connections == 1, "只有仍然活着的连接才该留在登记表里"

    def test_close_is_idempotent(self, db: Database) -> None:
        db.close()
        db.close()
        assert db.open_connections == 0

    def test_db_is_usable_again_after_close(self, db: Database, tmp_path: Path) -> None:
        """close 之后再取连接应当拿到一条可用的新连接，而不是炸掉。"""
        db.kv_set("k", "v1")
        db.close()
        assert db.kv_get("k") == "v1"

    def test_write_rolls_back_on_exception(self, db: Database) -> None:
        with pytest.raises(RuntimeError, match="boom"), db.write() as conn:
            conn.execute("INSERT INTO kv (key, value) VALUES ('x', 'y')")
            raise RuntimeError("boom")
        assert db.kv_get("x") is None
