"""鉴权与配额：令牌提取、密钥判定、按配置拦截、日配额。

关键设计（决定了这些断言的形状）：
* 默认免鉴权 —— 带错密钥也放行，但归到「未署名」桶。
* 开启 ``require_key`` 后才拦截，且区分「没带 / 带错 / 已停用」三种码。
* 配额查的是**本地自然日**，不是滚动 24 小时。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from openproxy.config import RuntimeConfig, Settings
from openproxy.domain import ANONYMOUS_LABEL, ProxyRejection, TokenUsage, UsageRecord
from openproxy.service.auth import (
    AuthService,
    KeyState,
    QuotaService,
    extract_bearer,
)
from openproxy.store import DAY_MS, Database, KeyStore, UsageStore, day_start_ms, now_ms

TZ = 480
BASE_TS = 1_791_028_800_000  # 2026-10-03 12:00Z = UTC+8 当天 20:00


class CaseInsensitiveHeaders(dict[str, str]):
    """最小的大小写不敏感映射，模拟 Starlette 的 ``Headers``。"""

    def __init__(self, data: dict[str, str]) -> None:
        super().__init__(data)
        self._lower = {k.lower(): v for k, v in data.items()}

    def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
        return self._lower.get(key.lower(), default)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "t.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def usage(db: Database) -> UsageStore:
    return UsageStore(db, tz_offset_minutes=TZ)


@pytest.fixture
def auth(db: Database, usage: UsageStore) -> AuthService:
    return AuthService(KeyStore(db), usage)


@pytest.fixture
def quota(usage: UsageStore) -> QuotaService:
    return QuotaService(usage)


def config(**kw: object) -> RuntimeConfig:
    return RuntimeConfig.compose(Settings(**kw))  # type: ignore[arg-type]


class TestExtractBearer:
    def test_standard_bearer(self) -> None:
        assert extract_bearer(CaseInsensitiveHeaders({"Authorization": "Bearer sk-abc"})) == "sk-abc"

    def test_lowercase_header_name(self) -> None:
        assert extract_bearer(CaseInsensitiveHeaders({"authorization": "Bearer sk-abc"})) == "sk-abc"

    def test_mixed_case_scheme(self) -> None:
        assert extract_bearer(CaseInsensitiveHeaders({"Authorization": "bearer sk-abc"})) == "sk-abc"

    def test_bare_token_without_scheme(self) -> None:
        """有些客户端直接塞裸令牌，不带 Bearer。"""
        assert extract_bearer(CaseInsensitiveHeaders({"Authorization": "sk-abc"})) == "sk-abc"

    def test_extra_whitespace(self) -> None:
        assert extract_bearer(CaseInsensitiveHeaders({"Authorization": "  Bearer   sk-abc  "})) == "sk-abc"

    @pytest.mark.parametrize("header", ["x-api-key", "api-key"])
    def test_api_key_headers_are_both_accepted(self, header: str) -> None:
        """``api-key``（无 x 前缀）也是常见写法；删掉它会让用这个头的客户端
        在开启密钥校验后静默变成匿名，然后收到 401。"""
        assert extract_bearer(CaseInsensitiveHeaders({header: "sk-op-abc"})) == "sk-op-abc"

    def test_authorization_wins_over_x_api_key(self) -> None:
        headers = CaseInsensitiveHeaders({"Authorization": "Bearer a", "X-Api-Key": "b"})
        assert extract_bearer(headers) == "a"

    def test_empty_and_absent(self) -> None:
        assert extract_bearer(CaseInsensitiveHeaders({})) == ""
        assert extract_bearer(CaseInsensitiveHeaders({"Authorization": "   "})) == ""
        assert extract_bearer(CaseInsensitiveHeaders({"X-Api-Key": ""})) == ""

    def test_bearer_with_empty_token(self) -> None:
        assert extract_bearer(CaseInsensitiveHeaders({"Authorization": "Bearer "})) == ""


class TestResolve:
    def test_no_header_is_anonymous(self, auth: AuthService) -> None:
        client = auth.resolve(CaseInsensitiveHeaders({}))
        assert client.state is KeyState.ANONYMOUS
        assert client.anonymous is True
        assert client.key_id is None
        assert client.label == ANONYMOUS_LABEL

    def test_valid_key(self, auth: AuthService) -> None:
        record, raw = auth._keys.create("甲")
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw}"}))
        assert client.state is KeyState.VALID
        assert client.key_id == record.id
        assert client.label == "甲"
        assert client.anonymous is False

    def test_garbage_key_is_unknown_not_anonymous(self, auth: AuthService) -> None:
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": "Bearer sk-fake"}))
        assert client.state is KeyState.UNKNOWN
        assert client.anonymous is True

    def test_disabled_key_reported_with_its_record(self, auth: AuthService) -> None:
        record, raw = auth._keys.create("甲")
        auth._keys.set_disabled(record.id, True)
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw}"}))
        assert client.state is KeyState.DISABLED
        assert client.key is not None and client.key.name == "甲"
        assert client.anonymous is True

    @pytest.mark.parametrize("header", ["X-Api-Key", "Api-Key"])
    def test_both_api_key_headers_reach_the_same_key(
        self, auth: AuthService, header: str
    ) -> None:
        """用签发时的**明文**分别走两种头，必须解析到同一张密钥。"""
        record, raw = auth._keys.create("甲")
        client = auth.resolve(CaseInsensitiveHeaders({header: raw}))
        assert client.state is KeyState.VALID
        assert client.key_id == record.id

    def test_upstream_key_is_never_valid_here(self, auth: AuthService) -> None:
        """上游的 ``sk-`` 密钥在本站必然查不到 —— 中转站不认上游凭证。"""
        assert auth.resolve(CaseInsensitiveHeaders({"Authorization": "Bearer sk-proj-x"})).state is (
            KeyState.UNKNOWN
        )


class TestEnforce:
    def test_open_mode_lets_everything_through(self, auth: AuthService) -> None:
        cfg = config(require_key=False)
        auth.enforce(auth.resolve(CaseInsensitiveHeaders({})), cfg)
        auth.enforce(auth.resolve(CaseInsensitiveHeaders({"Authorization": "Bearer bad"})), cfg)
        auth.enforce(auth.resolve(CaseInsensitiveHeaders({"Authorization": "Bearer sk-op-nope"})), cfg)

    def test_required_mode_rejects_missing_key(self, auth: AuthService) -> None:
        cfg = config(require_key=True)
        with pytest.raises(ProxyRejection) as exc:
            auth.enforce(auth.resolve(CaseInsensitiveHeaders({})), cfg)
        assert exc.value.status == 401
        assert exc.value.code == "missing_api_key"

    def test_required_mode_rejects_unknown_key(self, auth: AuthService) -> None:
        cfg = config(require_key=True)
        with pytest.raises(ProxyRejection) as exc:
            auth.enforce(auth.resolve(CaseInsensitiveHeaders({"Authorization": "Bearer sk-x"})), cfg)
        assert exc.value.code == "invalid_api_key"

    def test_required_mode_rejects_disabled_key(self, auth: AuthService) -> None:
        record, raw = auth._keys.create("甲")
        auth._keys.set_disabled(record.id, True)
        cfg = config(require_key=True)
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw}"}))
        with pytest.raises(ProxyRejection) as exc:
            auth.enforce(client, cfg)
        assert exc.value.code == "api_key_disabled"

    def test_valid_key_passes_even_when_required(self, auth: AuthService) -> None:
        _, raw = auth._keys.create("甲")
        cfg = config(require_key=True)
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw}"}))
        auth.enforce(client, cfg)  # 不抛异常即为通过

    def test_payload_shape_is_openai_compatible(self) -> None:
        payload = ProxyRejection(401, "invalid_api_key", "密钥无效").to_payload()
        assert payload == {"error": {"type": "invalid_api_key", "message": "密钥无效"}}


class TestQuota:
    def _spend(self, usage: UsageStore, tokens: int, key_id: str | None = None) -> None:
        usage.record(
            UsageRecord(
                ts=BASE_TS,
                model="m",
                path="/v1/chat/completions",
                stream=False,
                status=200,
                latency_ms=1,
                usage=TokenUsage(tokens, 0, 0, 0, tokens, True),
                key_id=key_id,
                key_label="甲" if key_id else "",
                anonymous=key_id is None,
            )
        )

    def test_under_quota_passes(self, auth: AuthService, quota: QuotaService) -> None:
        _, raw = auth._keys.create("甲", daily_token_quota=1000)
        self._spend(quota._usage, 999)
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw}"}))
        quota.check(client, config(), now=BASE_TS)

    def test_exact_quota_is_rejected(
        self, auth: AuthService, quota: QuotaService
    ) -> None:
        """``>=`` 而不是 ``>``：配额用满即止，否则会多放行一批。"""
        _, raw = auth._keys.create("甲", daily_token_quota=1000)
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw}"}))
        self._spend(quota._usage, 1000, client.key_id)
        with pytest.raises(ProxyRejection) as exc:
            quota.check(client, config(), now=BASE_TS)
        assert exc.value.status == 429
        assert exc.value.code == "daily_quota_exceeded"
        assert "1000" in exc.value.message

    def test_quota_is_per_key_not_shared(self, auth: AuthService, quota: QuotaService) -> None:
        _, raw_a = auth._keys.create("甲", daily_token_quota=100)
        _, raw_b = auth._keys.create("乙", daily_token_quota=100)
        client_a = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw_a}"}))
        self._spend(quota._usage, 100, client_a.key_id)
        client_b = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw_b}"}))
        quota.check(client_b, config(), now=BASE_TS)  # 乙没花过，不受甲影响

    def test_quota_window_is_local_calendar_day(
        self, auth: AuthService, quota: QuotaService
    ) -> None:
        """昨天的用量不该占用今天的配额。"""
        _, raw = auth._keys.create("甲", daily_token_quota=100)
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw}"}))
        self._spend(quota._usage, 100, client.key_id)
        quota.check(client, config(), now=BASE_TS + DAY_MS)  # 明天

    def test_unlimited_key_never_rejected(self, auth: AuthService, quota: QuotaService) -> None:
        _, raw = auth._keys.create("甲")
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw}"}))
        self._spend(quota._usage, 10**9, client.key_id)
        quota.check(client, config(), now=BASE_TS)

    def test_global_quota_applies_to_anonymous_traffic(
        self, quota: QuotaService
    ) -> None:
        from openproxy.service.auth import ResolvedClient

        anon = ResolvedClient(state=KeyState.ANONYMOUS)
        self._spend(quota._usage, 500)
        with pytest.raises(ProxyRejection) as exc:
            quota.check(anon, config(daily_token_quota=500), now=BASE_TS)
        assert exc.value.code == "global_quota_exceeded"

    def test_global_quota_zero_means_unlimited(self, quota: QuotaService) -> None:
        from openproxy.service.auth import ResolvedClient

        anon = ResolvedClient(state=KeyState.ANONYMOUS)
        self._spend(quota._usage, 10**9)
        quota.check(anon, config(daily_token_quota=0), now=BASE_TS)

    def test_snapshot_reports_remaining_headroom(
        self, auth: AuthService, quota: QuotaService
    ) -> None:
        _, raw = auth._keys.create("甲", daily_token_quota=1000)
        client = auth.resolve(CaseInsensitiveHeaders({"Authorization": f"Bearer {raw}"}))
        self._spend(quota._usage, 400, client.key_id)
        cfg = config(daily_token_quota=5000)
        snap = quota.snapshot(client, cfg, now=BASE_TS)
        assert snap["today_tokens"] == 400
        assert snap["daily_quota"] == 1000
        assert snap["global_today_tokens"] == 400
        assert snap["global_quota"] == 5000

    def test_snapshot_for_anonymous_has_no_per_key_numbers(self, quota: QuotaService) -> None:
        from openproxy.service.auth import ResolvedClient

        snap = quota.snapshot(ResolvedClient(state=KeyState.ANONYMOUS), config(), now=BASE_TS)
        assert snap["today_tokens"] is None
        assert snap["daily_quota"] is None

    def test_window_boundaries_are_local_midnight(self, quota: QuotaService) -> None:
        today = day_start_ms(BASE_TS, TZ)
        assert day_start_ms(today + DAY_MS - 1, TZ) == today
        assert day_start_ms(today + DAY_MS, TZ) == today + DAY_MS

    def test_zero_activity_snapshot_is_zero_not_error(self, quota: QuotaService) -> None:
        from openproxy.service.auth import ResolvedClient

        snap = quota.snapshot(ResolvedClient(state=KeyState.ANONYMOUS), config(), now=now_ms())
        assert snap["global_today_tokens"] == 0
