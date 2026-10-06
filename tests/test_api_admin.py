"""``/api/admin/*`` 控制台接口：读写、鉴权、参数校验、统计口径。"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from openproxy.config import Settings
from openproxy.container import Container
from openproxy.domain import ErrorKind, TokenUsage, UsageRecord
from openproxy.store import DAY_MS, UsageStore, day_start_ms, now_ms
from tests.support.upstream import FakeUpstream, refuse, standard_upstream
from tests.test_api_relay import _app

ADMIN = "/api/admin"
TZ = 480


@pytest.fixture
def upstream() -> FakeUpstream:
    return standard_upstream()


@pytest.fixture
def usage(client: TestClient) -> UsageStore:
    app = cast(Any, client.app)
    return cast(Container, app.state.container).usage


_SEQ = itertools.count()


def _kind_for(status: int) -> ErrorKind:
    """和代理层一致：非 2xx 归到 ``upstream_status``。"""
    return ErrorKind.NONE if status < 400 else ErrorKind.UPSTREAM_STATUS


def seed(usage: UsageStore, *, days_ago: int = 0, tokens: int = 10, status: int = 200,
         model: str = "space-bunny-free", key_id: str | None = None,
         label: str = "", stream: bool = False, known: bool = True,
         error_kind: ErrorKind | None = None) -> None:
    """写一条用量。

    ts 里带一个自增序号：否则同一分钟内写入的多条记录时间戳相同，分页断言会因为
    「看起来重叠」而失败（排序靠 ts + id，但断言只比了 ts）。
    """
    base = day_start_ms(now_ms(), TZ)
    usage.record(
        UsageRecord(
            ts=base - days_ago * DAY_MS + 3_600_000 + next(_SEQ),
            model=model,
            path="/v1/chat/completions",
            stream=stream,
            status=status,
            latency_ms=120,
            usage=TokenUsage(tokens, tokens // 2, 0, 0, tokens, known),
            key_id=key_id,
            key_label=label,
            anonymous=key_id is None,
            client_ip="127.0.0.1",
            error_kind=error_kind or _kind_for(status),
        )
    )


class TestOverview:
    def test_shape_on_an_empty_station(self, client: TestClient) -> None:
        payload = client.get(f"{ADMIN}/overview").json()
        assert set(payload) == {"today", "week", "total", "trend", "models", "recent",
                                "unknown_usage"}
        assert payload["today"]["requests"] == 0
        assert len(payload["trend"]) == 14

    def test_windows_nest(self, client: TestClient, usage: UsageStore) -> None:
        seed(usage, days_ago=0, tokens=10)
        seed(usage, days_ago=3, tokens=20)
        seed(usage, days_ago=40, tokens=40)
        payload = client.get(f"{ADMIN}/overview").json()
        assert payload["today"]["total_tokens"] == 10
        assert payload["week"]["total_tokens"] == 30
        assert payload["total"]["total_tokens"] == 70

    def test_trend_agrees_with_today(self, client: TestClient, usage: UsageStore) -> None:
        """R21：一个数据集、多个渲染器 —— 两个页面上的同一个数字必须相等。"""
        seed(usage, tokens=11)
        seed(usage, tokens=22)
        payload = client.get(f"{ADMIN}/overview").json()
        assert payload["trend"][-1]["total_tokens"] == payload["today"]["total_tokens"]
        assert payload["trend"][-1]["requests"] == payload["today"]["requests"]

    def test_model_breakdown_sums_to_the_same_total(self, client: TestClient, usage: UsageStore) -> None:
        seed(usage, tokens=10, model="space-bunny-free")
        seed(usage, tokens=20, model="ling-3.1-flash-free")
        payload = client.get(f"{ADMIN}/overview").json()
        assert sum(m["total_tokens"] for m in payload["models"]) == payload["today"]["total_tokens"]

    def test_trend_days_query_is_bounded(self, client: TestClient) -> None:
        assert len(client.get(f"{ADMIN}/overview?trend_days=30").json()["trend"]) == 30
        assert client.get(f"{ADMIN}/overview?trend_days=0").status_code == 422
        assert client.get(f"{ADMIN}/overview?trend_days=91").status_code == 422

    def test_unknown_usage_is_surfaced(self, client: TestClient, usage: UsageStore) -> None:
        seed(usage, tokens=0, known=False)
        payload = client.get(f"{ADMIN}/overview").json()
        assert payload["unknown_usage"] == 1
        assert payload["today"]["unknown_usage"] == 1


class TestUsageListing:
    def test_empty(self, client: TestClient) -> None:
        payload = client.get(f"{ADMIN}/usage").json()
        assert payload == {
            "items": [], "total": 0, "page": 1, "page_size": 20, "pages": 1,
            "models": [], "filters": {
                "model": None, "key_id": None, "status": "", "search": "",
                "anonymous_only": False, "since": None, "until": None,
            },
        }

    def test_pagination(self, client: TestClient, usage: UsageStore) -> None:
        for i in range(25):
            seed(usage, tokens=i + 1)
        first = client.get(f"{ADMIN}/usage?page=1&page_size=10").json()
        second = client.get(f"{ADMIN}/usage?page=2&page_size=10").json()
        assert (first["total"], first["pages"]) == (25, 3)
        assert len(second["items"]) == 10
        assert {i["ts"] for i in first["items"]} & {i["ts"] for i in second["items"]} == set()

    def test_filters(self, client: TestClient, usage: UsageStore) -> None:
        seed(usage, model="space-bunny-free", key_id="k1", label="甲")
        seed(usage, model="ling-3.1-flash-free", status=502)
        seed(usage, days_ago=5)
        assert client.get(f"{ADMIN}/usage?model=ling-3.1-flash-free").json()["total"] == 1
        assert client.get(f"{ADMIN}/usage?status=error").json()["total"] == 1
        assert client.get(f"{ADMIN}/usage?status=ok").json()["total"] == 2
        assert client.get(f"{ADMIN}/usage?key_id=k1").json()["total"] == 1
        assert client.get(f"{ADMIN}/usage?anonymous_only=true").json()["total"] == 2
        assert client.get(f"{ADMIN}/usage?search=ling").json()["total"] == 1

    def test_invalid_status_is_422(self, client: TestClient) -> None:
        assert client.get(f"{ADMIN}/usage?status=weird").status_code == 422

    def test_page_size_bounds(self, client: TestClient) -> None:
        assert client.get(f"{ADMIN}/usage?page_size=0").status_code == 422
        assert client.get(f"{ADMIN}/usage?page_size=201").status_code == 422
        assert client.get(f"{ADMIN}/usage?page=0").status_code == 422

    def test_search_length_is_capped(self, client: TestClient) -> None:
        assert client.get(f"{ADMIN}/usage?search=" + "x" * 201).status_code == 422

    def test_page_beyond_the_last_one_is_empty_not_an_error(
        self, client: TestClient, usage: UsageStore
    ) -> None:
        for i in range(5):
            seed(usage, tokens=i + 1)
        payload = client.get(f"{ADMIN}/usage?page=999&page_size=10").json()
        assert payload["items"] == []
        assert payload["total"] == 5
        assert payload["pages"] == 1
        assert payload["page"] == 999  # 如实回显，不偷偷改写

    def test_inverted_range_returns_nothing(self, client: TestClient, usage: UsageStore) -> None:
        """since > until 不是错误，只是一个空区间。"""
        now = now_ms()
        assert client.get(f"{ADMIN}/usage?since={now}&until={now - 86_400_000}").json()["total"] == 0

    @pytest.mark.parametrize(
        "query",
        ["page=1000000000000000000", "since=99999999999999999999999",
         "until=-99999999999999999999999"],
    )
    def test_out_of_range_numbers_do_not_500(self, client: TestClient, query: str) -> None:
        """超出 int64 的数字进 sqlite3 会抛 OverflowError → 500。上层夹一次。"""
        assert client.get(f"{ADMIN}/usage?{query}").status_code == 200

    def test_item_payload_fields(self, client: TestClient, usage: UsageStore) -> None:
        seed(usage, stream=True)
        item = client.get(f"{ADMIN}/usage").json()["items"][0]
        assert item["stream"] is True
        assert item["ok"] is True
        assert item["usage_known"] is True
        assert item["latency_ms"] == 120


class TestModelsPage:
    def test_lists_the_whole_catalog(self, client: TestClient) -> None:
        payload = client.get(f"{ADMIN}/models").json()
        assert len(payload["items"]) == 10
        assert payload["unlisted"] == []
        assert payload["probe"] is None
        assert {i["id"] for i in payload["items"]} >= {"space-bunny-free", "big-pickle"}

    def test_item_fields(self, client: TestClient) -> None:
        item = client.get(f"{ADMIN}/models").json()["items"][0]
        assert set(item) >= {"id", "name", "vendor", "note", "available", "latency_ms",
                             "requests", "total_tokens", "errors", "avg_latency_ms"}

    def test_usage_is_attached_per_model(self, client: TestClient, usage: UsageStore) -> None:
        seed(usage, model="space-bunny-free", tokens=77)
        items = {i["id"]: i for i in client.get(f"{ADMIN}/models?days=1").json()["items"]}
        assert items["space-bunny-free"]["total_tokens"] == 77
        assert items["space-bunny-free"]["requests"] == 1
        assert items["big-pickle"]["total_tokens"] == 0

    def test_probe_marks_availability(self, client: TestClient) -> None:
        client.post(f"{ADMIN}/channel/probe")
        items = {i["id"]: i for i in client.get(f"{ADMIN}/models").json()["items"]}
        assert items["space-bunny-free"]["available"] is True
        assert items["big-pickle"]["available"] is False

    def test_days_bounds(self, client: TestClient) -> None:
        assert client.get(f"{ADMIN}/models?days=0").status_code == 422
        assert client.get(f"{ADMIN}/models?days=366").status_code == 422


class TestChannelPage:
    def test_payload(self, client: TestClient, usage: UsageStore) -> None:
        seed(usage, status=502)
        seed(usage, days_ago=1)
        payload = client.get(f"{ADMIN}/channel?days=7").json()
        assert payload["upstream_base"] == "https://opencode.ai/zen"
        assert payload["window_days"] == 7
        assert payload["summary"]["errors"] == 1
        assert len(payload["status_trend"]) == 7
        assert payload["error_kinds"]["upstream_status"] == 1
        assert payload["today_window"]["end"] - payload["today_window"]["start"] == DAY_MS
        assert payload["admin_protected"] is False

    def test_probe_endpoint(self, client: TestClient) -> None:
        payload = client.post(f"{ADMIN}/channel/probe").json()
        assert payload["ok"] is True
        assert payload["upstream_count"] == 1

    def test_probe_after_failure_reports_not_ok(self, settings: Settings) -> None:
        from tests.test_api_relay import _app

        with TestClient(_app(settings, FakeUpstream(handlers={"*": refuse}))) as c:
            payload = c.post(f"{ADMIN}/channel/probe").json()
        assert payload["ok"] is False
        assert payload["detail"]

    def test_days_bounds(self, client: TestClient) -> None:
        assert client.get(f"{ADMIN}/channel?days=91").status_code == 422

    def test_admin_protected_flag_reports_the_dangerous_state(
        self, settings: Settings
    ) -> None:
        """渠道页的这个标记是 README 里「暴露到局域网」警告的依据。
        之前只在**没设令牌**时断言过（值为 False），硬编码成 False 全套件照样绿。"""
        import dataclasses

        with TestClient(
            _app(dataclasses.replace(settings, admin_token="ADM"), standard_upstream())
        ) as c:
            payload = c.get(f"{ADMIN}/channel", headers={"X-Admin-Token": "ADM"}).json()
            assert payload["admin_protected"] is True

    def test_admin_protected_flag_is_false_without_a_token(self, client: TestClient) -> None:
        assert client.get(f"{ADMIN}/channel").json()["admin_protected"] is False


class TestKeysCrud:
    def test_create_returns_the_secret_exactly_once(self, client: TestClient) -> None:
        response = client.post(f"{ADMIN}/keys", json={"name": "甲", "note": "测试"})
        assert response.status_code == 201
        payload = response.json()
        secret = payload["secret"]
        assert secret.startswith("sk-op-")
        assert payload["key"]["name"] == "甲"
        assert "唯一一次" in payload["hint"]

        listed = client.get(f"{ADMIN}/keys").json()
        assert "secret" not in json.dumps(listed)
        assert listed["items"][0]["prefix"] == secret[: len(listed["items"][0]["prefix"])]

    def test_secret_is_not_stored_in_plaintext(self, client: TestClient) -> None:
        secret = client.post(f"{ADMIN}/keys", json={"name": "甲"}).json()["secret"]
        body = json.dumps(client.get(f"{ADMIN}/keys").json())
        assert secret not in body

    def test_blank_name_is_defaulted(self, client: TestClient) -> None:
        assert client.post(f"{ADMIN}/keys", json={"name": "   "}).json()["key"]["name"] == (
            "未命名密钥"
        )

    def test_validation_errors(self, client: TestClient) -> None:
        assert client.post(f"{ADMIN}/keys", json={"name": "x" * 65}).status_code == 422
        assert client.post(f"{ADMIN}/keys", json={"daily_token_quota": 0}).status_code == 422

    def test_patch_rename_and_quota(self, client: TestClient) -> None:
        key_id = client.post(f"{ADMIN}/keys", json={"name": "甲"}).json()["key"]["id"]
        patched = client.patch(f"{ADMIN}/keys/{key_id}", json={"name": "乙", "daily_token_quota": 500})
        assert patched.status_code == 200
        assert patched.json()["name"] == "乙"
        assert patched.json()["daily_token_quota"] == 500

    def test_patch_quota_can_be_cleared(self, client: TestClient) -> None:
        """回归：``ge=1`` 时界面上「留空 = 不限」提交不上去，配额一旦设过就取消不掉。"""
        key_id = client.post(f"{ADMIN}/keys", json={"name": "甲"}).json()["key"]["id"]
        assert client.patch(f"{ADMIN}/keys/{key_id}",
                            json={"daily_token_quota": 500}).json()["daily_token_quota"] == 500
        cleared = client.patch(f"{ADMIN}/keys/{key_id}", json={"daily_token_quota": 0})
        assert cleared.status_code == 200
        assert cleared.json()["daily_token_quota"] is None

    def test_patch_negative_quota_is_422(self, client: TestClient) -> None:
        key_id = client.post(f"{ADMIN}/keys", json={"name": "甲"}).json()["key"]["id"]
        assert client.patch(f"{ADMIN}/keys/{key_id}",
                            json={"daily_token_quota": -1}).status_code == 422

    def test_patch_null_leaves_quota_untouched(self, client: TestClient) -> None:
        """``null`` = 不修改，与 ``0`` = 清除必须区分开。"""
        key_id = client.post(f"{ADMIN}/keys", json={"name": "甲"}).json()["key"]["id"]
        client.patch(f"{ADMIN}/keys/{key_id}", json={"daily_token_quota": 500})
        after = client.patch(f"{ADMIN}/keys/{key_id}", json={"daily_token_quota": None})
        assert after.json()["daily_token_quota"] == 500

    def test_patch_disable_and_enable(self, client: TestClient) -> None:
        key_id = client.post(f"{ADMIN}/keys", json={"name": "甲"}).json()["key"]["id"]
        assert client.patch(f"{ADMIN}/keys/{key_id}", json={"disabled": True}).json()["disabled"] is True
        assert client.patch(f"{ADMIN}/keys/{key_id}", json={"disabled": False}).json()["disabled"] is False

    def test_patch_missing_key_is_404(self, client: TestClient) -> None:
        assert client.patch(f"{ADMIN}/keys/nope", json={"name": "x"}).status_code == 404

    def test_delete_keeps_usage_history(self, client: TestClient, usage: UsageStore) -> None:
        created = client.post(f"{ADMIN}/keys", json={"name": "甲"}).json()
        secret = created["secret"]
        seed(usage, key_id=created["key"]["id"], label="甲")
        response = client.delete(f"{ADMIN}/keys/{created['key']['id']}")
        assert response.status_code == 200
        assert response.json()["kept_usage_records"] == 1
        assert client.get(f"{ADMIN}/keys").json()["total"] == 0
        assert client.get(f"{ADMIN}/usage").json()["total"] == 1
        assert secret not in json.dumps(response.json())

    def test_delete_missing_key_is_404(self, client: TestClient) -> None:
        assert client.delete(f"{ADMIN}/keys/nope").status_code == 404

    def test_list_counts_active(self, client: TestClient) -> None:
        a = client.post(f"{ADMIN}/keys", json={"name": "甲"}).json()["key"]["id"]
        client.post(f"{ADMIN}/keys", json={"name": "乙"})
        client.patch(f"{ADMIN}/keys/{a}", json={"disabled": True})
        payload = client.get(f"{ADMIN}/keys").json()
        assert (payload["total"], payload["active"]) == (2, 1)

    def test_stats_window_follows_retain_days(self, client: TestClient) -> None:
        """窗口写死 90 天的话，保留 7 天时页面会显示「有数据」而记录其实早被清了。"""
        assert client.get(f"{ADMIN}/keys").json()["window_days"] == 90
        client.patch(f"{ADMIN}/settings", json={"retain_days": 7})
        assert client.get(f"{ADMIN}/keys").json()["window_days"] == 7

    def test_renamed_key_does_not_split_its_usage(
        self, client: TestClient, usage: UsageStore
    ) -> None:
        """回归：``GROUP BY key_id, label`` 会把改过名的密钥拆成两桶，
        页面按 key_id 建字典只留最后一行 → 用量少报。"""
        created = client.post(f"{ADMIN}/keys", json={"name": "旧名字"}).json()["key"]
        for _ in range(3):
            seed(usage, key_id=created["id"], label="旧名字")
        client.patch(f"{ADMIN}/keys/{created['id']}", json={"name": "新名字"})
        seed(usage, key_id=created["id"], label="新名字")

        item = client.get(f"{ADMIN}/keys").json()["items"][0]
        assert item["name"] == "新名字"
        assert item["requests"] == 4, "改名不该把历史用量拆走"
        assert item["total_tokens"] == 4 * 10

    def test_list_carries_usage_stats(self, client: TestClient, usage: UsageStore) -> None:
        created = client.post(f"{ADMIN}/keys", json={"name": "甲"}).json()["key"]
        seed(usage, tokens=42, key_id=created["id"], label="甲")
        item = client.get(f"{ADMIN}/keys").json()["items"][0]
        assert item["requests"] == 1
        assert item["total_tokens"] == 42


class TestSettings:
    def test_read_exposes_everything_the_ui_needs(self, client: TestClient) -> None:
        payload = client.get(f"{ADMIN}/settings").json()
        assert set(payload) >= {
            "host", "port", "upstream_base", "upstream_authenticated", "upstream_user_agent",
            "require_key", "admin_protected", "retain_days", "max_body_bytes",
            "free_models_only", "inject_stream_usage", "daily_token_quota",
            "overlays", "base_url_hint", "free_model_ids", "probe",
        }
        assert payload["base_url_hint"].endswith("/v1")
        assert len(payload["free_model_ids"]) == 10

    def test_secrets_are_never_exposed(self, settings: Settings, tmp_path: Path) -> None:
        import dataclasses

        from tests.test_api_relay import _app

        tuned = dataclasses.replace(
            settings, upstream_key="sk-HIDE-ME", admin_token="ADM-HIDE-ME"
        )
        with TestClient(_app(tuned, standard_upstream())) as c:
            body = c.get(f"{ADMIN}/settings").text
        assert "sk-HIDE-ME" not in body
        assert "ADM-HIDE-ME" not in body

    def test_patch_applies(self, client: TestClient) -> None:
        response = client.patch(f"{ADMIN}/settings", json={"require_key": True, "retain_days": 14})
        assert response.status_code == 200
        assert response.json()["require_key"] is True
        assert client.get("/v1/__health").json()["require_key"] is True

    def test_patch_persists_across_app_instances(self, settings: Settings, tmp_path: Path) -> None:
        from tests.test_api_relay import _app

        db = str(tmp_path / "shared.db")
        for _ in range(2):
            app = _app(_with_db(settings, db), standard_upstream())
            with TestClient(app) as c:
                c.patch(f"{ADMIN}/settings", json={"retain_days": 11})
        app = _app(_with_db(settings, db), standard_upstream())
        with TestClient(app) as c:
            assert c.get(f"{ADMIN}/settings").json()["retain_days"] == 11

    def test_patch_rejects_bad_values(self, client: TestClient) -> None:
        assert client.patch(f"{ADMIN}/settings", json={"retain_days": 0}).status_code == 422
        assert client.patch(f"{ADMIN}/settings", json={"upstream_base": "ftp://x"}).status_code == 422

    def test_blank_upstream_base_falls_back_to_the_baseline(self, client: TestClient) -> None:
        """留空要真的能清掉覆盖层 —— 控制台上就是这么承诺的。"""
        client.patch(f"{ADMIN}/settings", json={"upstream_base": "http://127.0.0.1:9/zen"})
        assert client.get(f"{ADMIN}/settings").json()["overlays"]["upstream_base"] is not None
        cleared = client.patch(f"{ADMIN}/settings", json={"upstream_base": ""})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["overlays"]["upstream_base"] is None
        assert cleared.json()["upstream_base"] == Settings().upstream_base
        # 清掉之后其它覆盖层不受影响（这正是「恢复默认」做不到的那部分价值）
        client.patch(f"{ADMIN}/settings", json={"retain_days": 21})
        again = client.patch(f"{ADMIN}/settings", json={"upstream_base": "  "})
        assert again.status_code == 200
        assert again.json()["overlays"]["retain_days"] == 21

    def test_patch_with_empty_body_is_400(self, client: TestClient) -> None:
        assert client.patch(f"{ADMIN}/settings", json={}).status_code == 400

    def test_explicit_null_is_not_a_change(self, client: TestClient) -> None:
        """``{"require_key": null}`` = 「不改这个字段」，不是「清掉这个覆盖层」。

        靠 ``exclude_unset=True``：请求体里**出现过**的字段才算改动。换成
        ``exclude_none=True`` 的话，界面把某个下拉框发成 null 就会把已设的覆盖层
        悄悄清掉，而 ``{"retain_days": null}`` 甚至会因为「没有改动」报 400。
        """
        client.patch(f"{ADMIN}/settings", json={"require_key": True, "retain_days": 5})
        for payload in ({"require_key": None}, {"retain_days": None}, {"upstream_base": None}):
            response = client.patch(f"{ADMIN}/settings", json=payload)
            assert response.status_code == 400, payload
        after = client.get(f"{ADMIN}/settings").json()
        assert after["require_key"] is True
        assert after["retain_days"] == 5

    def test_zero_quota_survives_the_unset_filter(self, client: TestClient) -> None:
        """对照组：``0`` 是真值，不能被 ``exclude_none`` 之类的过滤器顺手滤掉。"""
        response = client.patch(f"{ADMIN}/settings", json={"daily_token_quota": 0})
        assert response.status_code == 200
        assert response.json()["daily_token_quota"] == 0

    def test_reset_restores_baseline(self, client: TestClient) -> None:
        client.patch(f"{ADMIN}/settings", json={"require_key": True, "retain_days": 5})
        payload = client.post(f"{ADMIN}/settings/reset").json()
        assert payload["require_key"] is False
        assert payload["retain_days"] == 90

    def test_patch_zero_quota_is_allowed(self, client: TestClient) -> None:
        assert client.patch(f"{ADMIN}/settings", json={"daily_token_quota": 0}).status_code == 200


class TestAdminToken:
    def test_open_when_no_token_configured(self, settings: Settings) -> None:
        from tests.test_api_relay import _app

        with TestClient(_app(settings, standard_upstream())) as c:
            assert c.get(f"{ADMIN}/overview").status_code == 200

    def test_locked_when_token_configured(self, settings: Settings) -> None:
        import dataclasses

        from tests.test_api_relay import _app

        with TestClient(_app(dataclasses.replace(settings, admin_token="ADM"), standard_upstream())) as c:
            assert c.get(f"{ADMIN}/overview").status_code == 401
            assert c.get(f"{ADMIN}/overview", headers={"X-Admin-Token": "wrong"}).status_code == 401
            assert c.get(f"{ADMIN}/overview", headers={"X-Admin-Token": "ADM"}).status_code == 200
            assert c.get(
                f"{ADMIN}/overview", headers={"Authorization": "Bearer ADM"}
            ).status_code == 200
            assert c.get(
                f"{ADMIN}/overview", headers={"Authorization": "ADM"}
            ).status_code == 200

    def test_relay_path_is_not_gated_by_the_admin_token(self, settings: Settings) -> None:
        import dataclasses

        from tests.test_api_relay import _app

        with TestClient(_app(dataclasses.replace(settings, admin_token="ADM"), standard_upstream())) as c:
            assert c.get("/v1/__health").status_code == 200
            assert c.post(
                "/v1/chat/completions", json={"model": "space-bunny-free", "messages": []}
            ).status_code == 200

    def test_admin_protected_flag_is_visible(self, settings: Settings) -> None:
        import dataclasses

        from tests.test_api_relay import _app

        with TestClient(_app(dataclasses.replace(settings, admin_token="ADM"), standard_upstream())) as c:
            payload = c.get(f"{ADMIN}/settings", headers={"X-Admin-Token": "ADM"}).json()
        assert payload["admin_protected"] is True


class TestSettingsContract:
    """「界面上每个开关都必须真的能改」这条契约。

    之前 ``free_models_only`` 与 ``inject_stream_usage`` 两个开关点了报
    ``400 没有需要修改的字段``：PATCH 模型里没有这两个字段，Pydantic 静默丢弃，
    请求体被判为空。开关外观与能用的开关一模一样，所以纯靠手点很难发现 ——
    下面第一条测试就是把它钉死的那条。
    """

    def test_every_editable_field_is_actually_patchable(self, client: TestClient) -> None:
        """逐个 PATCH ``/settings`` 声明可改的每个字段，必须 2xx 且真的生效。"""
        editable = client.get(f"{ADMIN}/settings").json()["editable"]
        assert editable, "/settings 必须声明哪些字段可改"

        samples: dict[str, object] = {
            "require_key": True,
            "free_models_only": False,
            "inject_stream_usage": False,
            "reasoning_effort": "high",
            "retain_days": 45,
            "daily_token_quota": 123_456,
            "upstream_base": "http://127.0.0.1:9/zen",
            # 列表类字段给**非空**样本：给 [] 的话「字段被 Pydantic 丢弃」与
            # 「字段被接受但存成空」都表现为 200，测不出差别。
            "opencode_models": ["big-pickle", "fledge-alpha-free"],
        }
        for field in editable:
            assert field in samples, f"{field} 可改但没有对应的测试样本"
            response = client.patch(f"{ADMIN}/settings", json={field: samples[field]})
            assert response.status_code == 200, (
                f"开关 {field} 改不动：HTTP {response.status_code} {response.text}"
            )
            assert response.json()[field] == samples[field], f"{field} 没真的生效"

    def test_editable_list_matches_both_the_patch_model_and_the_overlays(self) -> None:
        """「声明可改」「PATCH 模型接受」「覆盖层有对应字段」三者必须是同一个集合。

        多一个 → 控制台出现点不动的开关（正是上一轮的实际故障）；
        少一个 → 界面上某个开关报「没有需要修改的字段」。
        """
        from openproxy.api.routes_admin import SettingsPatch
        from openproxy.config import EDITABLE_FIELDS, Overlays

        patchable = set(SettingsPatch.model_fields)
        overlay_fields = set(Overlays._FIELD_NAMES)
        assert patchable == overlay_fields, (
            f"仅 PATCH 模型有 {sorted(patchable - overlay_fields)}，"
            f"仅覆盖层有 {sorted(overlay_fields - patchable)}"
        )
        assert set(EDITABLE_FIELDS) == overlay_fields

    def test_read_settings_advertises_the_editable_list(self, client: TestClient) -> None:
        assert client.get(f"{ADMIN}/settings").json()["editable"] == list(editable_of())

    def test_overlays_persist_all_six_fields(self, client: TestClient) -> None:
        for field, value in (
            ("require_key", True),
            ("free_models_only", False),
            ("inject_stream_usage", False),
            ("retain_days", 45),
            ("daily_token_quota", 999),
        ):
            client.patch(f"{ADMIN}/settings", json={field: value})
        overlays = client.get(f"{ADMIN}/settings").json()["overlays"]
        assert overlays["require_key"] is True
        assert overlays["free_models_only"] is False
        assert overlays["inject_stream_usage"] is False
        assert overlays["retain_days"] == 45
        assert overlays["daily_token_quota"] == 999

    def test_reset_clears_the_two_new_overlays(self, client: TestClient) -> None:
        client.patch(f"{ADMIN}/settings",
                     json={"free_models_only": False, "inject_stream_usage": False})
        payload = client.post(f"{ADMIN}/settings/reset").json()
        assert payload["free_models_only"] is True
        assert payload["inject_stream_usage"] is True


class TestMaintenance:
    def test_flush_is_idempotent(self, client: TestClient) -> None:
        payload = client.post(f"{ADMIN}/maintenance/flush").json()
        assert payload["flushed"] is True
        assert client.post(f"{ADMIN}/maintenance/flush").json()["flushed"] is True

    def test_flush_reports_the_true_counters(self, client: TestClient) -> None:
        """README 把 ``/maintenance/flush`` 称作「权威口径」，那就得真的准。

        之前只断言了空闲时的 ``flushed is True``，于是四个计数器
        （``written``/``failed``/``dropped``/``pending``）随便返回什么常量都算通过。
        这里先真发两次调用（一成功一失败），再断言三个数字与库里对得上。
        """
        assert client.post("/v1/chat/completions",
                           json={"model": "space-bunny-free", "messages": []}).status_code == 200
        assert client.post("/v1/chat/completions",
                           json={"model": "gpt-5.6-sol", "messages": []}).status_code == 400
        payload = client.post(f"{ADMIN}/maintenance/flush").json()
        assert payload["flushed"] is True
        assert payload["pending"] == 0, "flush 之后队列里不该还剩东西"
        assert payload["written"] == 2, "成功与被拒都要记账，flush 报的就是记账条数"
        assert payload["failed"] == 0
        assert payload["dropped"] == 0
        # 硬约定：written + dropped + failed == record() 被接受的总数
        assert payload["written"] + payload["dropped"] + payload["failed"] == 2

    def test_flush_counters_are_cumulative(self, client: TestClient) -> None:
        """第二次 flush 不能把上一次的 written 抹成 0。"""
        client.post("/v1/chat/completions",
                    json={"model": "space-bunny-free", "messages": []})
        first = client.post(f"{ADMIN}/maintenance/flush").json()
        assert first["written"] == 1
        second = client.post(f"{ADMIN}/maintenance/flush").json()
        assert second["written"] == 1, "累计计数不是本轮的增量"
        assert second["pending"] == 0

    def test_flush_reports_a_real_drop_count(self, settings: Settings) -> None:
        """``dropped`` 要跟着记账器走，不能写死 0。

        真要靠队列塞满来制造丢弃，得先堵住写线程，太重；这里直接改计数器的值，
        断言的仍然是端点**读的是记账器**这件事 —— 而「丢弃」在生产里恰恰是运维
        唯一能看到的丢统计信号，写死 0 等于把它焊死。
        """
        from openproxy.app import create_app

        app = create_app(settings, transport=standard_upstream(), start_pruner=False,
                         start_prober=False, tz_offset_minutes=480)
        with TestClient(app) as c:
            c.app.state.container.recorder.dropped = 5  # type: ignore[attr-defined]
            payload = c.post(f"{ADMIN}/maintenance/flush").json()
            assert payload["flushed"] is True
            assert payload["dropped"] == 5

    def test_prune_removes_old_records(self, client: TestClient, usage: UsageStore) -> None:
        seed(usage, days_ago=200)
        seed(usage, days_ago=0)
        payload = client.post(f"{ADMIN}/maintenance/prune").json()
        assert payload["removed"] == 1
        assert client.get(f"{ADMIN}/usage").json()["total"] == 1

    def test_prune_respects_the_configured_window(self, client: TestClient, usage: UsageStore) -> None:
        seed(usage, days_ago=20)
        client.patch(f"{ADMIN}/settings", json={"retain_days": 10})
        assert client.post(f"{ADMIN}/maintenance/prune").json()["removed"] == 1

    def test_prune_on_empty_station(self, client: TestClient) -> None:
        assert client.post(f"{ADMIN}/maintenance/prune").json()["removed"] == 0

    def test_prune_boundary_is_exact(self, client: TestClient, usage: UsageStore) -> None:
        """``ts < cutoff`` 与 ``ts <= cutoff`` 的差别必须钉死，否则「N 天」
        契约在边界上含糊。"""
        client.patch(f"{ADMIN}/settings", json={"retain_days": 10})
        cutoff = client.get(f"{ADMIN}/channel").json()["today_window"]["start"] - 10 * DAY_MS
        usage.record(_at(cutoff - 1))   # 刚过期 → 删
        usage.record(_at(cutoff))       # 正好在边界上 → 留
        usage.record(_at(cutoff + 1))   # 未过期 → 留
        assert client.post(f"{ADMIN}/maintenance/prune").json()["removed"] == 1
        assert client.get(f"{ADMIN}/usage?page_size=10").json()["total"] == 2

    def test_startup_prunes_expired_records(self, settings: Settings) -> None:
        """启动时那一次清理是 retain_days 的唯一生效点（除了 6 小时循环）。"""
        import dataclasses

        from openproxy.app import create_app

        c1 = _app(settings, standard_upstream())
        with TestClient(c1) as c:
            seed(c.app.state.container.usage, days_ago=200)  # type: ignore[attr-defined]
            seed(c.app.state.container.usage, days_ago=0)     # type: ignore[attr-defined]
        assert _count(settings) == 2

        # 保留窗口收窄到 30 天后重启 → 启动清理应删掉那条 200 天前的
        c2 = create_app(
            dataclasses.replace(settings, retain_days=30),
            transport=standard_upstream(),
            start_pruner=False,
            start_prober=False,
            tz_offset_minutes=480,
        )
        with TestClient(c2):
            pass
        assert _count(settings) == 1


def _at(ts: int) -> UsageRecord:
    """造一条指定时间戳的用量。"""
    return UsageRecord(
        ts=ts, model="m", path="/p", stream=False, status=200, latency_ms=1,
        usage=TokenUsage(10, 0, 0, 0, 10, True),
    )


def _count(settings: Settings) -> int:
    """数库里总共有多少条用量（绕过写入队列，直接读库）。

    ``with sqlite3.connect(...)`` **只管事务，不关连接** —— 连接要到 GC 才回收，
    而 Python 3.14 会为未关闭的连接发 ``ResourceWarning``，在本项目
    ``filterwarnings = error`` 下变成测试失败。所以显式 ``close()``。
    """
    import sqlite3

    conn = sqlite3.connect(settings.db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0])
    finally:
        conn.close()


class TestClientContext:
    def test_anonymous_admin_sees_no_per_key_numbers(self, client: TestClient) -> None:
        payload = client.get(f"{ADMIN}/client-context").json()
        assert payload["today_tokens"] is None
        assert payload["daily_quota"] is None

    def test_admin_with_a_key_sees_its_own_quota(self, client: TestClient) -> None:
        secret = client.post(f"{ADMIN}/keys", json={"name": "甲", "daily_token_quota": 1000}).json()[
            "secret"
        ]
        payload = client.get(
            f"{ADMIN}/client-context", headers={"Authorization": f"Bearer {secret}"}
        ).json()
        assert payload["daily_quota"] == 1000
        assert payload["today_tokens"] == 0


def editable_of() -> tuple[str, ...]:
    from openproxy.config import EDITABLE_FIELDS

    return EDITABLE_FIELDS


def _with_db(settings: Settings, db: str) -> Settings:
    import dataclasses

    return dataclasses.replace(settings, db_path=db)
