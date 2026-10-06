"""密钥仓储：签发、查找、停用、删除、配额，以及「明文只出现一次」。"""

from __future__ import annotations

import string
from collections.abc import Iterator
from pathlib import Path

import pytest

from openproxy.domain import ApiKey, TokenUsage, UsageFilter, UsageRecord
from openproxy.store import KEY_PREFIX, Database, KeyStore, UsageStore, hash_key, new_key_material


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "t.db")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def keys(db: Database) -> KeyStore:
    return KeyStore(db)


def must_get(keys: KeyStore, key_id: str) -> ApiKey:
    """``KeyStore.get`` 返回 Optional；测试里「刚创建的密钥必然存在」是前提，
    显式断言比到处写 ``or key`` 更能说清意图。"""
    found = keys.get(key_id)
    assert found is not None, f"密钥不存在: {key_id}"
    return found


class TestKeyMaterial:
    def test_prefix_and_shape(self) -> None:
        raw, _key_id, preview = new_key_material()
        assert raw.startswith(KEY_PREFIX)
        assert preview == raw[: len(preview)]
        assert len(raw) == len(KEY_PREFIX) + 32  # 24 字节熵 → 32 个 base64url 字符
        secret = raw[len(KEY_PREFIX) :]
        assert set(secret) <= set(string.ascii_letters + string.digits + "-_")

    def test_hash_is_stable_and_hex32(self) -> None:
        raw, key_id, _ = new_key_material()
        assert key_id == hash_key(raw)
        assert len(key_id) == 32
        assert all(c in "0123456789abcdef" for c in key_id)

    def test_raw_is_not_recoverable_from_id(self) -> None:
        raw, key_id, _ = new_key_material()
        assert raw not in key_id
        assert hash_key(key_id) != key_id

    def test_each_key_is_unique(self) -> None:
        ids = {new_key_material()[1] for _ in range(50)}
        assert len(ids) == 50


class TestCreate:
    def test_returns_record_and_raw_once(self, keys: KeyStore) -> None:
        record, raw = keys.create("甲")
        assert record.name == "甲"
        assert record.disabled is False
        assert record.daily_token_quota is None
        assert keys.resolve(raw) is not None

    def test_blank_name_gets_a_default(self, keys: KeyStore) -> None:
        record, _ = keys.create("   ")
        assert record.name == "未命名密钥"

    def test_name_is_trimmed(self, keys: KeyStore) -> None:
        record, _ = keys.create("  甲  ")
        assert record.name == "甲"

    def test_overlong_name_rejected(self, keys: KeyStore) -> None:
        with pytest.raises(ValueError, match="64"):
            keys.create("x" * 65)

    def test_non_positive_quota_rejected(self, keys: KeyStore) -> None:
        with pytest.raises(ValueError, match="daily_token_quota"):
            keys.create("甲", daily_token_quota=0)
        with pytest.raises(ValueError, match="daily_token_quota"):
            keys.create("甲", daily_token_quota=-5)

    def test_note_is_capped(self, keys: KeyStore) -> None:
        """截断必须发生在**写进去的那一行**上。

        ``create()`` 返回的数据类是入参派生的，所以只断言它等于 200 字，
        去掉 INSERT 里的 ``[:200]`` 照样绿 —— 而库里存的是 500 字，
        读回来还是 500。所以要断言 ``get()`` 读回来的那一行。
        """
        record, _ = keys.create("甲", note="n" * 500)
        stored = keys.get(record.id)
        assert stored is not None
        assert len(stored.note) == 200
        assert stored.note == "n" * 200


class TestResolve:
    def test_resolves_valid_key(self, keys: KeyStore) -> None:
        record, raw = keys.create("甲")
        found = keys.resolve(raw)
        assert found is not None
        assert found.id == record.id

    def test_surrounding_whitespace_tolerated(self, keys: KeyStore) -> None:
        _, raw = keys.create("甲")
        assert keys.resolve(f"  {raw}  ") is not None

    def test_empty_string_is_anonymous_not_error(self, keys: KeyStore) -> None:
        assert keys.resolve("") is None
        assert keys.resolve("   ") is None

    def test_foreign_prefix_short_circuits(self, keys: KeyStore) -> None:
        """非本站前缀（含上游的 ``sk-``）不该浪费一次哈希，更不该被误认。"""
        assert keys.resolve("sk-abcdef") is None
        assert keys.resolve("Bearer sk-op-xxx") is None

    def test_wrong_secret_of_right_shape_is_not_found(self, keys: KeyStore) -> None:
        keys.create("甲")
        forged = KEY_PREFIX + "x" * 32
        assert keys.resolve(forged) is None

    def test_lookup_is_case_sensitive(self, keys: KeyStore) -> None:
        _, raw = keys.create("甲")
        assert keys.resolve(raw.upper()) is None


class TestMutations:
    def test_disable_then_enable_round_trip(self, keys: KeyStore) -> None:
        record, _ = keys.create("甲")
        assert keys.set_disabled(record.id, True) is True
        assert must_get(keys, record.id).disabled is True
        assert keys.set_disabled(record.id, False) is True
        assert must_get(keys, record.id).disabled is False
        assert must_get(keys, record.id).disabled_at is None

    def test_mutating_missing_key_reports_miss(self, keys: KeyStore) -> None:
        assert keys.set_disabled("nope", True) is False
        assert keys.delete("nope") is False
        assert keys.set_quota("nope", 10) is False
        assert keys.rename("nope", "x") is False

    def test_rename_validates(self, keys: KeyStore) -> None:
        record, _ = keys.create("甲")
        with pytest.raises(ValueError, match="1–64"):
            keys.rename(record.id, "   ")
        with pytest.raises(ValueError, match="1–64"):
            keys.rename(record.id, "y" * 65)
        assert keys.rename(record.id, "乙") is True
        assert must_get(keys, record.id).name == "乙"

    def test_quota_can_be_set_and_cleared(self, keys: KeyStore) -> None:
        record, _ = keys.create("甲")
        assert keys.set_quota(record.id, 500) is True
        assert must_get(keys, record.id).daily_token_quota == 500
        assert keys.set_quota(record.id, None) is True
        assert must_get(keys, record.id).daily_token_quota is None

    def test_quota_rejects_non_positive(self, keys: KeyStore) -> None:
        record, _ = keys.create("甲")
        with pytest.raises(ValueError, match="quota"):
            keys.set_quota(record.id, 0)

    def test_list_ordering_and_disabled_filter(self, keys: KeyStore) -> None:
        a, _ = keys.create("甲", created_at=1000)
        b, _ = keys.create("乙", created_at=2000)
        keys.set_disabled(a.id, True)
        assert [k.id for k in keys.list()] == [b.id, a.id]  # created_at DESC
        assert [k.id for k in keys.list(include_disabled=False)] == [b.id]

    def test_count(self, keys: KeyStore) -> None:
        assert keys.count() == 0
        keys.create("甲")
        keys.create("乙")
        assert keys.count() == 2


class TestUsageSurvivesKeyDeletion:
    def test_deleting_a_key_keeps_its_usage_records(
        self, db: Database, keys: KeyStore
    ) -> None:
        """报表不能因为一次误删密钥而出现空洞 —— 这是建表时**不加外键**的原因。"""
        usage = UsageStore(db, tz_offset_minutes=0)
        record, _ = keys.create("甲")
        usage.record(
            UsageRecord(
                ts=1_791_028_800_000,
                model="space-bunny-free",
                path="/v1/chat/completions",
                stream=False,
                status=200,
                latency_ms=10,
                usage=TokenUsage(1, 1, 0, 0, 2, True),
                key_id=record.id,
                key_label="甲",
                anonymous=False,
            )
        )
        assert keys.delete(record.id) is True
        assert keys.get(record.id) is None
        kept = usage.list(UsageFilter(key_id=record.id))
        assert kept.total == 1
        assert kept.items[0].key_label == "甲"  # 名称是当时的快照
