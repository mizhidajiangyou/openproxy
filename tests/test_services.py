"""模型目录、运行期配置覆盖、仪表盘聚合。"""

from __future__ import annotations

import dataclasses
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from openproxy.config import ConfigError, Overlays, Settings
from openproxy.domain import (
    FREE_MODEL_IDS,
    FREE_MODELS,
    ErrorKind,
    TokenUsage,
    UsageFilter,
    UsageRecord,
)
from openproxy.service.config_service import (
    ConfigService,
    decode_overlays,
    encode_overlays,
)
from openproxy.service.dashboard import DashboardService
from openproxy.service.model_catalog import ModelCatalog, parse_model_ids
from openproxy.store import DAY_MS, Database, UsageStore, day_start_ms, now_ms

TZ = 480


# ------------------------------------------------------------- 模型目录 ---


class TestParseModelIds:
    def test_happy_path(self) -> None:
        payload = {"data": [{"id": "a"}, {"id": "b"}, {"id": "a"}]}
        assert parse_model_ids(payload) == frozenset({"a", "b"})

    @pytest.mark.parametrize(
        "payload", [None, {}, [], "x", {"data": None}, {"data": {}}, {"data": [1, "a", {}]}]
    )
    def test_malformed_payloads_degrade_to_empty(self, payload: object) -> None:
        assert parse_model_ids(payload) == frozenset()


class TestFreeModelCatalog:
    def test_catalog_has_ten_models(self) -> None:
        assert len(FREE_MODELS) == 10
        assert len(FREE_MODEL_IDS) == 10

    def test_ids_are_unique_and_sorted_in_source(self) -> None:
        assert len(FREE_MODEL_IDS) == len(FREE_MODELS)
        assert [m.model_id for m in FREE_MODELS] == sorted(m.model_id for m in FREE_MODELS)

    def test_every_model_has_a_display_name_and_vendor(self) -> None:
        assert all(m.display_name and m.vendor for m in FREE_MODELS)

    def test_allowlist_gates_when_free_only(self) -> None:
        c = ModelCatalog()
        assert c.is_allowed("space-bunny-free", free_only=True) is True
        assert c.is_allowed("gpt-5.6-sol", free_only=True) is False
        assert c.is_allowed("gpt-5.6-sol", free_only=False) is True

    def test_entries_before_any_probe_report_unknown_availability(self) -> None:
        entries = ModelCatalog().entries()
        assert len(entries) == 10
        assert all(e.available is None and e.latency_ms is None for e in entries)

    def test_entries_merge_usage_stats(self) -> None:
        entries = ModelCatalog().entries({"space-bunny-free": {"requests": 3, "total_tokens": 99}})
        target = next(e for e in entries if e.model.model_id == "space-bunny-free")
        assert (target.requests, target.total_tokens) == (3, 99)

    def test_entries_ignore_stats_for_models_not_in_the_catalog(self) -> None:
        entries = ModelCatalog().entries({"mystery-model": {"requests": 5}})
        assert sum(e.requests for e in entries) == 0


class TestProbe:
    async def test_success_records_availability(self) -> None:
        payload = {"data": [{"id": "space-bunny-free"}, {"id": "other"}]}
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=payload)

        c = ModelCatalog()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await c.probe(client, "https://example.test/zen")
        assert result.ok is True
        assert result.available_ids == frozenset({"space-bunny-free", "other"})
        assert c.last_probe is not None and c.last_probe.ok is True
        entries = c.entries()
        by_id = {e.model.model_id: e for e in entries}
        assert by_id["space-bunny-free"].available is True
        assert by_id["ling-3.1-flash-free"].available is False
        assert calls["n"] == 1

    async def test_retries_once_on_transient_failure(self) -> None:
        """``GET /v1/models`` 是幂等的，重试一次是安全的。"""
        state = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            state["n"] += 1
            if state["n"] == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"data": []})

        c = ModelCatalog()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await c.probe(client, "https://example.test")
        assert result.ok is True
        assert state["n"] == 2

    async def test_gives_up_after_two_attempts(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        c = ModelCatalog()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await c.probe(client, "https://example.test")
        assert result.ok is False
        assert result.available_ids == frozenset()
        assert "网络错误" in result.detail
        # 探测失败后可用性必须是「未知」而不是「全部离线」
        assert all(e.available is None for e in c.entries())

    async def test_error_status_is_retried_then_reported(self) -> None:
        """上游回 5xx 也要重试一次（README 与使用指南都写了「失败会自动重试一次」）。

        之前只断言「结果是失败」，没数调用次数 —— 把 5xx 分支的 ``continue`` 改成
        ``break``（于是 4xx/5xx 一律不重试）整套件照样绿。
        """
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503, json={})

        c = ModelCatalog()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await c.probe(client, "https://example.test")
        assert result.ok is False
        assert calls["n"] == 2, "5xx 必须重试一次，和连接类失败一样"
        # 两个条件都要成立。之前写的是 `status == 0 or "503" in ...`：`status`
        # 恒为 0，左半边永远为真，右半边**从不被求值** —— 「把上游状态码写进
        # detail」这件事其实一次都没被验证（换成固定文案照样绿）。
        assert result.status == 0
        assert "503" in result.detail

    async def test_non_json_body_is_a_failure(self) -> None:
        c = ModelCatalog()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"})
            )
        ) as client:
            result = await c.probe(client, "https://example.test")
        assert result.ok is False
        assert "JSON" in result.detail

    async def test_public_payload_shape(self) -> None:
        c = ModelCatalog()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"data": []}))
        ) as client:
            await c.probe(client, "https://example.test")
        payload = c.last_probe.to_public()  # type: ignore[union-attr]
        assert set(payload) == {
            "ok", "latency_ms", "checked_at", "detail", "status",
            "upstream_count", "reachability",
        }
        # 默认不跑可达性探测（它每轮要发 10 次真实请求），必须是 None 而不是空字典
        assert payload["reachability"] is None


# --------------------------------------------------------- 运行期配置覆盖 ---


class TestOverlayCodec:
    def test_round_trip(self) -> None:
        original = Overlays(require_key=True, upstream_base="http://x", retain_days=5,
                            daily_token_quota=99)
        assert decode_overlays(encode_overlays(original)) == original

    def test_blank_and_broken_json_degrade_to_empty(self) -> None:
        assert decode_overlays("") == Overlays()
        assert decode_overlays(None) == Overlays()
        assert decode_overlays("{not json") == Overlays()
        assert decode_overlays("[1,2]") == Overlays()

    def test_wrong_types_in_json_are_ignored_not_crashed(self) -> None:
        raw = json.dumps(
            {"require_key": "yes", "upstream_base": 5, "retain_days": "ten", "daily_token_quota": True}
        )
        assert decode_overlays(raw) == Overlays()

    def test_blank_upstream_base_becomes_none(self) -> None:
        assert decode_overlays(json.dumps({"upstream_base": "  "})).upstream_base is None


class TestConfigService:
    @pytest.fixture
    def db(self, tmp_path: Path) -> Iterator[Database]:
        database = Database(tmp_path / "c.db")
        database.migrate()
        yield database
        database.close()

    def test_starts_from_baseline(self, db: Database) -> None:
        svc = ConfigService(db, Settings(require_key=False, retain_days=30))
        assert svc.snapshot.require_key is False
        assert svc.snapshot.retain_days == 30

    def test_patch_applies_and_persists(self, db: Database) -> None:
        svc = ConfigService(db, Settings())
        svc.patch(require_key=True)
        assert svc.snapshot.require_key is True
        # 新实例（模拟重启）读回同一个值
        assert ConfigService(db, Settings()).snapshot.require_key is True

    def test_patch_leaves_other_fields_alone(self, db: Database) -> None:
        svc = ConfigService(db, Settings())
        svc.patch(retain_days=7)
        svc.patch(require_key=True)
        assert svc.snapshot.retain_days == 7
        assert svc.snapshot.require_key is True

    def test_invalid_patch_is_rejected_and_state_unchanged(self, db: Database) -> None:
        svc = ConfigService(db, Settings(retain_days=30))
        with pytest.raises(ConfigError):
            svc.patch(retain_days=0)
        assert svc.snapshot.retain_days == 30

    def test_rejected_patch_does_not_persist(self, db: Database) -> None:
        svc = ConfigService(db, Settings())
        with pytest.raises(ConfigError):
            svc.patch(upstream_base="ftp://bad")
        assert ConfigService(db, Settings()).snapshot.upstream_base == Settings().upstream_base

    def test_reset_returns_to_baseline(self, db: Database) -> None:
        svc = ConfigService(db, Settings(retain_days=30))
        svc.patch(retain_days=7, require_key=True)
        svc.reset_overlays()
        assert svc.snapshot.retain_days == 30
        assert svc.snapshot.require_key is False

    def test_reload_from_db_picks_up_external_writes(self, db: Database) -> None:
        other = ConfigService(db, Settings())
        other.patch(retain_days=11)
        mine = ConfigService(db, Settings())
        assert mine.snapshot.retain_days == 11  # 构造时就读到了

    def test_concurrent_patches_do_not_lose_each_other(self, db: Database) -> None:
        """回归：读-改-写曾经是「锁内读、锁外写」，两个并发 patch 各自拿到同一份
        旧快照，后写的把先写的整个覆盖层顶掉 —— 改一个字段会连带丢掉另一个。"""
        svc = ConfigService(db, Settings())
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def patch(field: str, value: object) -> None:
            try:
                barrier.wait(5)
                svc.patch(**{field: value})
            except BaseException as exc:  # 收集起来统一断言
                errors.append(exc)

        threads = [
            threading.Thread(target=patch, args=("require_key", True)),
            threading.Thread(target=patch, args=("retain_days", 7)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert errors == []
        snapshot = svc.snapshot
        assert snapshot.require_key is True
        assert snapshot.retain_days == 7, "并发 patch 丢了其中一个字段"
        # 落库的值也必须同时含两个字段
        assert ConfigService(db, Settings()).snapshot.retain_days == 7

    def test_snapshot_is_immutable(self, db: Database) -> None:
        svc = ConfigService(db, Settings())
        with pytest.raises(dataclasses.FrozenInstanceError):
            svc.snapshot.port = 1  # type: ignore[misc]

    def test_public_dict_hides_secrets(self, db: Database) -> None:
        svc = ConfigService(db, Settings(upstream_key="sk-HIDEME"))
        assert "sk-HIDEME" not in json.dumps(svc.snapshot.public_dict())


# ----------------------------------------------------------- 仪表盘聚合 ---


class TestDashboardService:
    @pytest.fixture
    def db(self, tmp_path: Path) -> Iterator[Database]:
        database = Database(tmp_path / "d.db")
        database.migrate()
        yield database
        database.close()

    @pytest.fixture
    def usage(self, db: Database) -> UsageStore:
        return UsageStore(db, tz_offset_minutes=TZ)

    @pytest.fixture
    def dashboard(self, usage: UsageStore) -> DashboardService:
        return DashboardService(usage, ModelCatalog())

    def _seed(self, usage: UsageStore, days_ago: int, *, tokens: int, status: int = 200) -> None:
        base = day_start_ms(now_ms(), TZ)
        ts = base - days_ago * DAY_MS + 3_600_000
        usage.record(
            UsageRecord(
                ts=ts,
                model="space-bunny-free",
                path="/v1/chat/completions",
                stream=False,
                status=status,
                latency_ms=100,
                usage=TokenUsage(tokens, 0, 0, 0, tokens, True),
            )
        )

    def test_empty_database_gives_zeroed_but_well_formed_payload(
        self, dashboard: DashboardService
    ) -> None:
        payload = dashboard.overview()
        assert set(payload) == {
            "today", "week", "total", "trend", "models", "recent", "unknown_usage"
        }
        assert payload["today"]["requests"] == 0
        assert payload["today"]["error_rate"] == 0.0
        assert len(payload["trend"]) == 14
        assert payload["recent"] == []

    def test_today_week_total_windows_nest_correctly(
        self, dashboard: DashboardService, usage: UsageStore
    ) -> None:
        self._seed(usage, 0, tokens=10)
        self._seed(usage, 3, tokens=20)
        self._seed(usage, 40, tokens=40)
        payload = dashboard.overview()
        assert payload["today"]["total_tokens"] == 10
        assert payload["week"]["total_tokens"] == 30  # 含今天
        assert payload["total"]["total_tokens"] == 70  # 含全部

    def test_totals_agree_across_pages(self, dashboard: DashboardService, usage: UsageStore) -> None:
        """R21：一个数据集、多个渲染器 —— 总览的「今日」必须等于按日聚合的那一天。"""
        self._seed(usage, 0, tokens=10)
        self._seed(usage, 0, tokens=15)
        overview = dashboard.overview()
        today_bucket = overview["trend"][-1]
        assert today_bucket["total_tokens"] == overview["today"]["total_tokens"]
        assert today_bucket["requests"] == overview["today"]["requests"]

    def test_model_stats_match_the_summary(self, dashboard: DashboardService, usage: UsageStore) -> None:
        self._seed(usage, 0, tokens=10)
        self._seed(usage, 0, tokens=20)
        models = dashboard.models(days=1)["items"]
        total = sum(m["total_tokens"] for m in models)
        assert total == dashboard.overview()["today"]["total_tokens"]

    def test_unknown_usage_flag_surfaces_at_the_top(
        self, dashboard: DashboardService, usage: UsageStore
    ) -> None:
        usage.record(
            UsageRecord(
                ts=now_ms(),
                model="m",
                path="/p",
                stream=False,
                status=200,
                latency_ms=1,
                usage=TokenUsage.unknown(),
            )
        )
        payload = dashboard.overview()
        assert payload["unknown_usage"] == 1
        assert payload["total"]["unknown_usage"] == 1

    def test_usage_page_echoes_normalised_filters(self, dashboard: DashboardService) -> None:
        payload = dashboard.usage_page(UsageFilter(page=0, page_size=9999, status="bogus"))
        assert payload["page"] == 1
        assert payload["page_size"] == 200
        assert payload["filters"]["status"] == ""

    def test_unlisted_models_are_surfaced_separately(
        self, dashboard: DashboardService, usage: UsageStore
    ) -> None:
        usage.record(
            UsageRecord(
                ts=now_ms(),
                model="mystery",
                path="/p",
                stream=False,
                status=200,
                latency_ms=1,
                usage=TokenUsage(5, 5, 0, 0, 10, True),
            )
        )
        payload = dashboard.models(days=1)
        # 目录永远列全 10 个免费模型（主键 "id"）；清单外的用量单独归到 unlisted
        assert len(payload["items"]) == 10
        assert all(m["requests"] == 0 for m in payload["items"])
        assert [m["model"] for m in payload["unlisted"]] == ["mystery"]

    def test_channel_payload_shape(self, dashboard: DashboardService, usage: UsageStore) -> None:
        self._seed(usage, 0, tokens=5, status=502)
        payload = dashboard.channel(days=7, upstream_base="http://x")
        assert payload["upstream_base"] == "http://x"
        assert payload["summary"]["errors"] == 1
        assert len(payload["status_trend"]) == 7
        assert payload["probe"] is None

    def test_week_window_is_seven_days_not_six(
        self, dashboard: DashboardService, usage: UsageStore
    ) -> None:
        """「本周」= 含今天的 7 个自然日，所以 6 天前那条**必须**在里面。

        把 ``_window(7)`` 写成 ``_window(6)`` 时，之前那条只埋了 0/3/40 天的用例照样绿
        （3 天前两边都算），卡片上的数字会静默少一天。
        """
        self._seed(usage, 0, tokens=10)
        self._seed(usage, 5, tokens=20)
        self._seed(usage, 6, tokens=40)   # 第 7 天：只有 7 天的窗口收得下
        self._seed(usage, 7, tokens=80)   # 第 8 天：任何 7 天窗口都收不下
        payload = dashboard.overview()
        assert payload["week"]["requests"] == 3
        assert payload["week"]["total_tokens"] == 70

    def test_channel_error_kinds_respect_the_window(
        self, dashboard: DashboardService, usage: UsageStore
    ) -> None:
        """渠道页的失败原因拆解必须和同页其它数字用同一个时间窗。"""
        now = now_ms()
        for ts, kind in (
            (now, ErrorKind.UPSTREAM_TIMEOUT),
            (now - 10 * DAY_MS, ErrorKind.UPSTREAM_UNREACHABLE),
        ):
            usage.record(
                UsageRecord(
                    ts=ts,
                    model="space-bunny-free",
                    path="/v1/chat/completions",
                    stream=False,
                    status=502,
                    latency_ms=10,
                    usage=TokenUsage.unknown(),
                    error_kind=kind,
                )
            )
        payload = dashboard.channel(days=7, upstream_base="http://x")
        assert payload["error_kinds"] == {"upstream_timeout": 1}
        assert payload["summary"]["errors"] == 1

    def test_models_page_carries_usage_for_every_catalog_model(
        self, dashboard: DashboardService, usage: UsageStore
    ) -> None:
        """10 个免费模型都要带上自己的用量。

        ``by_model(..., limit=1)`` 会让只有 token 最多的那个模型有数、其余 9 个显示 0 ——
        在页面上就是「其余模型今天没人用过」。
        """
        now = now_ms()
        used = sorted(FREE_MODEL_IDS)[:4]
        for i, model_id in enumerate(used):
            usage.record(
                UsageRecord(
                    ts=now - i,
                    model=model_id,
                    path="/v1/chat/completions",
                    stream=False,
                    status=200,
                    latency_ms=1,
                    usage=TokenUsage(1, 1, 0, 0, 2, True),
                )
            )
        items = {m["id"]: m for m in dashboard.models(days=1)["items"]}
        for model_id in FREE_MODEL_IDS:
            expected = 2 if model_id in used else 0
            assert items[model_id]["total_tokens"] == expected, model_id
        assert dashboard.models(days=1)["unlisted"] == []

    def test_by_key_groups_anonymous(self, dashboard: DashboardService, usage: UsageStore) -> None:
        self._seed(usage, 0, tokens=5)
        rows = dashboard.by_key(days=1)
        assert len(rows) == 1 and rows[0]["anonymous"] is True

    def test_trend_days_are_clamped_at_the_store(self, dashboard: DashboardService) -> None:
        """服务层把 days 钉在 1..365；路由层还会再钉一次 1..90。"""
        assert len(dashboard.overview(trend_days=0)["trend"]) == 1
        assert len(dashboard.overview(trend_days=999)["trend"]) == 365

    def test_recent_is_limited_to_eight(self, dashboard: DashboardService, usage: UsageStore) -> None:
        base = day_start_ms(now_ms(), TZ)
        for i in range(20):
            usage.record(
                UsageRecord(
                    ts=base + i * 60_000,
                    model="m",
                    path="/p",
                    stream=False,
                    status=200,
                    latency_ms=1,
                    usage=TokenUsage(1, 0, 0, 0, 1, True),
                )
            )
        assert len(dashboard.overview()["recent"]) == 8

    def test_window_helpers_use_local_days(self, dashboard: DashboardService) -> None:
        start, end = dashboard._window(1)
        assert end - start == DAY_MS
        assert day_start_ms(start, TZ) == start
