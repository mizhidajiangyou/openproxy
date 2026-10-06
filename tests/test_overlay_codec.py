"""覆盖层的编解码往返。

**这一组测试是为了钉死一类具体缺陷**：``Overlays`` 加了字段但
``encode_overlays`` /``decode_overlays`` 忘了改，于是合成时读得到、落库时没写 ——
控制台开关点得动、返回 200，**重启就失效**。HTTP 层的测试全都绿，因为它们不重启。

所以这里的核心断言不是「某个值对不对」，而是**「集合相等」**：
编解码覆盖的字段必须恰好等于 ``Overlays`` 的字段集，多一个少一个都算失败。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from openproxy.config import Overlays, RuntimeConfig, Settings, load_settings
from openproxy.service.config_service import (
    ConfigService,
    decode_overlays,
    encode_overlays,
)
from openproxy.store import Database

NON_TRIVIAL = Overlays(
    require_key=True,
    upstream_base="http://example.test/zen",
    retain_days=45,
    daily_token_quota=123_456,
    free_models_only=False,
    inject_stream_usage=False,
    reasoning_effort="high",
    # 必须给「非平凡值」而不是空元组：空元组编出来是空数组，
    # 而「字段在不在」的断言对空值不敏感 —— 那样这个字段漏了编码也能过。
    opencode_models=("big-pickle", "fledge-alpha-free"),
)


class TestCodecCoverage:
    def test_encoded_keys_match_the_dataclass_fields(self) -> None:
        """漏写一个字段 = 它永远不落库。这条断言是那道闸。"""
        fields = {f.name for f in dataclasses.fields(NON_TRIVIAL)}
        assert set(json.loads(encode_overlays(NON_TRIVIAL))) == fields

    def test_decoded_keys_match_the_dataclass_fields(self) -> None:
        """多写一个字段 = 读回来被静默丢掉。"""
        fields = {f.name for f in dataclasses.fields(NON_TRIVIAL)}
        raw = json.dumps(dict.fromkeys(fields))
        assert {f.name for f in dataclasses.fields(decode_overlays(raw))} == fields

    def test_every_field_survives_a_round_trip(self) -> None:
        """逐字段断言，而不是整体相等 —— 整体相等在「两边同时漏一个」时会通过。"""
        back = decode_overlays(encode_overlays(NON_TRIVIAL))
        for f in dataclasses.fields(NON_TRIVIAL):
            assert getattr(back, f.name) == getattr(NON_TRIVIAL, f.name), (
                f"字段 {f.name} 没能在编解码里活下来"
            )

    def test_a_new_field_would_be_caught(self) -> None:
        """变异回归：故意让编解码漏掉一个字段，这条断言必须变红。"""
        raw = json.loads(encode_overlays(NON_TRIVIAL))
        del raw["reasoning_effort"]
        assert set(json.loads(encode_overlays(NON_TRIVIAL))) != set(raw)


class TestDecodeRobustness:
    def test_blank_and_broken_json_degrade_to_empty(self) -> None:
        for raw in (None, "", "   ", "not json", "[]", '"str"', "null", "123"):
            assert decode_overlays(raw) == Overlays()

    def test_wrong_types_degrade_to_none(self) -> None:
        """类型不对的值退化成「没设」，而不是让进程起不来。"""
        raw = json.dumps(
            {
                "require_key": "yes",
                "retain_days": "45",
                "daily_token_quota": 1.5,
                "reasoning_effort": 7,
                "upstream_base": ["http://x"],
            }
        )
        assert decode_overlays(raw) == Overlays()

    def test_bool_is_not_read_as_int(self) -> None:
        """``isinstance(True, int)`` 成立 —— 不排除 bool 的话 ``retain_days=True``
        会变成保留 1 天。"""
        raw = json.dumps({"retain_days": True, "daily_token_quota": False})
        back = decode_overlays(raw)
        assert back.retain_days is None
        assert back.daily_token_quota is None

    def test_blank_overlay_values_mean_unset(self) -> None:
        """``upstream_base`` 的空白串读回来必须是 ``None``（= 回基线）。

        ``reasoning_effort`` **不一样**：它的空串是「显式关闭强制」，
        必须原样保留成 ``""`` —— 读成 ``None`` 的话「用户在界面关掉开关」
        会在重启后被撤销（``None`` = 没设过 → 环境变量复活）。
        见 ``test_reasoning_effort.py::test_blank_survives_a_restart``。
        """
        raw = json.dumps({"upstream_base": "  ", "reasoning_effort": "  "})
        back = decode_overlays(raw)
        assert back.upstream_base is None
        assert back.reasoning_effort == ""

    def test_absent_reasoning_effort_key_means_unset(self) -> None:
        """键**不存在**与「键存在但值是空串」必须能区分 ——
        前者 = 没设过（回基线），后者 = 显式关闭。三态塌成两态就完了。"""
        assert decode_overlays(json.dumps({"require_key": True})).reasoning_effort is None
        assert decode_overlays(json.dumps({"reasoning_effort": ""})).reasoning_effort == ""
        assert decode_overlays(json.dumps({"reasoning_effort": None})).reasoning_effort is None

    def test_unknown_keys_are_ignored(self) -> None:
        """库里存了将来才有的字段时，老版本不该起不来。"""
        raw = json.dumps({"require_key": True, "some_future_field": 1})
        assert decode_overlays(raw).require_key is True


class TestPersistence:
    def _config(self, settings: Settings) -> ConfigService:
        db = Database(Path(settings.db_path))
        db.migrate()
        self._db = db
        return ConfigService(db, settings)

    def test_patch_reaches_the_database(self, settings: Settings) -> None:
        """配置必须真的落库 —— 只改内存快照的话，重启即失效。"""
        cfg = self._config(settings)
        cfg.patch(reasoning_effort="medium", retain_days=30)
        raw = self._db.kv_get("runtime_overlays")
        assert raw is not None
        stored = json.loads(raw)
        assert stored["reasoning_effort"] == "medium"
        assert stored["retain_days"] == 30
        self._db.close()

    def test_a_fresh_service_sees_the_patched_value(self, settings: Settings) -> None:
        cfg = self._config(settings)
        cfg.patch(reasoning_effort="high", free_models_only=False)
        self._db.close()
        # 重新开一个 ConfigService = 模拟重启
        db2 = Database(Path(settings.db_path))
        db2.migrate()
        try:
            fresh = ConfigService(db2, settings)
            assert fresh.snapshot.reasoning_effort == "high"
            assert fresh.snapshot.free_models_only is False
        finally:
            db2.close()

    def test_reset_clears_everything(self, settings: Settings) -> None:
        cfg = self._config(settings)
        cfg.patch(reasoning_effort="high")
        cfg.reset_overlays()
        stored = json.loads(self._db.kv_get("runtime_overlays") or "{}")
        assert stored["reasoning_effort"] is None
        assert cfg.snapshot.reasoning_effort is None
        self._db.close()

    def test_invalid_patch_leaves_the_stored_value_untouched(self, settings: Settings) -> None:
        """校验失败必须**在落库之前**就拒 —— 否则库里留下一个启动时才炸的值。"""
        from openproxy.config import ConfigError

        cfg = self._config(settings)
        cfg.patch(reasoning_effort="low")
        with pytest.raises(ConfigError):
            cfg.patch(reasoning_effort="none")
        assert cfg.snapshot.reasoning_effort == "low"
        assert (json.loads(self._db.kv_get("runtime_overlays") or "{}")["reasoning_effort"] == "low")
        self._db.close()

    def test_compose_uses_the_environment_baseline_when_unset(self) -> None:
        base = load_settings({"OPENPROXY_REASONING_EFFORT": "low"})
        rc = RuntimeConfig.compose(base, Overlays())
        assert rc.reasoning_effort == "low"
