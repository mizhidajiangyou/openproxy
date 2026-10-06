"""配置层：环境解析、覆盖层合成、边界与非法值。"""

from __future__ import annotations

from pathlib import Path

import pytest

from openproxy.config import (
    DEFAULT_UPSTREAM_BASE,
    ConfigError,
    Overlays,
    RuntimeConfig,
    Settings,
    load_dotenv,
    load_settings,
    merged_env,
)


class TestLoadSettings:
    def test_defaults_when_env_empty(self) -> None:
        s = load_settings({})
        assert s.host == "127.0.0.1"
        assert s.port == 8787
        assert s.upstream_base == DEFAULT_UPSTREAM_BASE
        assert s.upstream_key is None
        assert s.require_key is False
        assert s.admin_token is None
        assert s.retain_days == 90
        assert s.free_models_only is True
        assert s.inject_stream_usage is True

    def test_reads_every_key(self) -> None:
        env = {
            "OPENPROXY_HOST": "0.0.0.0",
            "OPENPROXY_PORT": "9000",
            "OPENPROXY_UPSTREAM_BASE": "http://127.0.0.1:11434",
            "OPENPROXY_UPSTREAM_KEY": "sk-up",
            "OPENPROXY_UPSTREAM_USER_AGENT": "curl/8.7.1",
            "OPENPROXY_REQUIRE_KEY": "true",
            "OPENPROXY_ADMIN_TOKEN": "t0ken",
            "OPENPROXY_DB_PATH": "/tmp/x.db",
            "OPENPROXY_CONNECT_TIMEOUT": "3",
            "OPENPROXY_READ_TIMEOUT": "42",
            "OPENPROXY_RETAIN_DAYS": "7",
            "OPENPROXY_MAX_BODY_BYTES": "2048",
            "OPENPROXY_FREE_MODELS_ONLY": "no",
            "OPENPROXY_INJECT_STREAM_USAGE": "0",
            "OPENPROXY_DAILY_TOKEN_QUOTA": "5000",
        }
        s = load_settings(env)
        assert s.host == "0.0.0.0"
        assert s.port == 9000
        assert s.upstream_base == "http://127.0.0.1:11434"
        assert s.upstream_key == "sk-up"
        assert s.upstream_user_agent == "curl/8.7.1"
        assert s.require_key is True
        assert s.admin_token == "t0ken"
        assert s.connect_timeout == 3.0
        assert s.read_timeout == 42.0
        assert s.retain_days == 7
        assert s.max_body_bytes == 2048
        assert s.free_models_only is False
        assert s.inject_stream_usage is False
        assert s.daily_token_quota == 5000

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1", True), ("true", True), ("YES", True), ("on", True),
         ("0", False), ("false", False), ("No", False), ("off", False)],
    )
    def test_flag_forms(self, raw: str, expected: bool) -> None:
        assert load_settings({"OPENPROXY_REQUIRE_KEY": raw}).require_key is expected

    def test_flag_rejects_garbage(self) -> None:
        with pytest.raises(ConfigError, match="布尔值"):
            load_settings({"OPENPROXY_REQUIRE_KEY": "maybe"})

    def test_blank_values_fall_back_to_default(self) -> None:
        s = load_settings(
            {"OPENPROXY_HOST": "  ", "OPENPROXY_PORT": "", "OPENPROXY_RETAIN_DAYS": " "}
        )
        assert s.host == "127.0.0.1"
        assert s.port == 8787
        assert s.retain_days == 90

    def test_blank_optional_key_becomes_none(self) -> None:
        assert load_settings({"OPENPROXY_UPSTREAM_KEY": "   "}).upstream_key is None

    def test_non_numeric_port_names_the_key(self) -> None:
        with pytest.raises(ConfigError, match="OPENPROXY_PORT"):
            load_settings({"OPENPROXY_PORT": "eight"})

    def test_non_numeric_timeout_names_the_key(self) -> None:
        with pytest.raises(ConfigError, match="OPENPROXY_CONNECT_TIMEOUT"):
            load_settings({"OPENPROXY_CONNECT_TIMEOUT": "soon"})

    @pytest.mark.parametrize("port", ["0", "65536", "-1"])
    def test_port_out_of_range(self, port: str) -> None:
        with pytest.raises(ConfigError, match="port"):
            load_settings({"OPENPROXY_PORT": port})

    def test_upstream_base_must_be_http(self) -> None:
        with pytest.raises(ConfigError, match="upstream_base"):
            load_settings({"OPENPROXY_UPSTREAM_BASE": "ftp://x"})

    @pytest.mark.parametrize("days", [0, -1, 3651])
    def test_retain_days_range_is_enforced_at_construction(self, days: int) -> None:
        """``retain_days=0`` 会让清理循环**清空全部历史**，必须在构造期就拒绝。

        直接构造 ``Settings(...)`` 的路径（测试、嵌入式调用）绕过了 ``load_settings``
        的取值转换，只有 ``__post_init__`` 里的 ``_check_range`` 拦得住。
        """
        with pytest.raises(ConfigError, match="retain_days"):
            Settings(retain_days=days)
        with pytest.raises(ConfigError, match="retain_days"):
            load_settings({"OPENPROXY_RETAIN_DAYS": str(days)})

    @pytest.mark.parametrize("size", [1023, (1 << 30) + 1])
    def test_max_body_bytes_range_is_enforced_at_construction(self, size: int) -> None:
        with pytest.raises(ConfigError, match="max_body_bytes"):
            Settings(max_body_bytes=size)

    def test_blank_user_agent_falls_back_to_default(self) -> None:
        """空白 UA 会让上游 Cloudflare 回 403（error code 1010），所以不能采信它。"""
        assert load_settings({"OPENPROXY_UPSTREAM_USER_AGENT": "  "}).upstream_user_agent == (
            Settings().upstream_user_agent
        )

    def test_explicitly_empty_user_agent_rejected_at_construction(self) -> None:
        """绕过 load_settings 直接构造时仍要拦住空 UA。"""
        with pytest.raises(ConfigError, match="upstream_user_agent"):
            Settings(upstream_user_agent="")


class TestDotenv:
    """``.env`` 文件的解析与优先级。

    这里的每条规则都对应一个真实的坑：

    - **优先级是「真实环境变量 > ``.env``」**，与 python-dotenv 的默认**相反**。
      反过来的话 ``docker run -e OPENPROXY_PORT=9000`` 就压不过文件里的值，
      容器编排怎么传参都没用。
    - **坏行跳过而不是抛异常** —— ``.env`` 里一个笔误不该让服务起不来。
    - **只在 :func:`load_settings` 里读**，不在 import 期读，否则测试会被
      开发者本机的 ``.env`` 污染（R15）。
    """

    def _write(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / ".env"
        p.write_text(content, encoding="utf-8")
        return p

    def test_basic_pairs(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, (
            "# 注释行\n"
            "\n"
            "OPENPROXY_PORT=10490\n"
            "OPENPROXY_OPENCODE_BASE=http://127.0.0.1:10490\n"
        ))
        parsed = load_dotenv(p)
        assert parsed == {
            "OPENPROXY_PORT": "10490",
            "OPENPROXY_OPENCODE_BASE": "http://127.0.0.1:10490",
        }

    def test_quotes_are_stripped(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, (
            'A="双引号"\n'
            "B='单引号'\n"
            'C="未闭合\n'
            'D=值带"引号\n'
            'E="\n'                # 单字符引号值
            "F=''\n"               # 两个单引号 = 空串
        ))
        parsed = load_dotenv(p)
        assert parsed["A"] == "双引号"
        assert parsed["B"] == "单引号"
        # 未闭合的引号**保留原样** —— 那更可能是值的一部分
        assert parsed["C"] == '"未闭合'
        assert parsed["D"] == '值带"引号'
        # **单字符引号不是成对的**（review 的 A-7）：`len(value) >= 2` 那个
        # 条件就是为它准备的。少了它，`value[1:-1]` 会把那个字符吞掉 ——
        # `E="` 会变成空串，静默丢字符。
        assert parsed["E"] == '"', "单字符引号应原样保留"
        assert parsed["F"] == "", "两个单引号是成对的，应剥成空串"

    def test_bom_does_not_break_the_first_key(self, tmp_path: Path) -> None:
        """**UTF-8 BOM 不能让第一个键失效**（review 的 B-1，P0）。

        Windows 记事本 / VS Code「以 UTF-8 with BOM 保存」是默认行为，
        而 ``utf-8`` 不剥 BOM -> 第一个键变成 ``\\ufeffOPENPROXY_PORT``，
        一个永远匹配不上任何键名的垃圾。症状**完全静默**：
        ``.env`` 写着 10490，``--print-config`` 打印 8787。
        """
        p = tmp_path / ".env"
        p.write_bytes(
            b"\xef\xbb\xbf"                        # UTF-8 BOM
            + b"OPENPROXY_PORT=10490\n"
            + b"OPENPROXY_HOST=127.0.0.1\n"
        )
        parsed = load_dotenv(p)
        assert parsed == {
            "OPENPROXY_PORT": "10490", "OPENPROXY_HOST": "127.0.0.1",
        }, f"BOM 让第一个键变成了 {list(parsed)!r}"

    def test_bom_settings_actually_take_effect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BOM 那条要落到**配置值**上才算出问题 —— 上一层只测解析。"""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_bytes(
            b"\xef\xbb\xbfOPENPROXY_PORT=10490\n"
        )
        for key in ("OPENPROXY_PORT",):
            monkeypatch.delenv(key, raising=False)
        assert load_settings().port == 10490, "带 BOM 的 .env 未生效"

    def test_export_prefix_tolerated(self, tmp_path: Path) -> None:
        """``export KEY=V`` 是常见的复制粘贴产物 —— 容忍而不是报错。"""
        p = self._write(tmp_path, "export OPENPROXY_PORT=9000\n")
        assert load_dotenv(p) == {"OPENPROXY_PORT": "9000"}

    def test_bad_lines_skipped_not_fatal(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, (
            "没有等号的行\n"
            "=值没有键\n"
            "OPENPROXY_PORT=8080\n"
        ))
        parsed = load_dotenv(p)
        assert parsed == {"OPENPROXY_PORT": "8080"}, "坏行应被跳过，好的行仍要生效"

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_dotenv(tmp_path / "不存在.env") == {}

    def test_empty_value_is_kept(self, tmp_path: Path) -> None:
        """``KEY=`` 要解析成空串而不是被丢掉。

        对 ``OPENPROXY_ADMIN_TOKEN=`` 这种「显式留空」有意义。
        """
        p = self._write(tmp_path, "OPENPROXY_ADMIN_TOKEN=\n")
        assert load_dotenv(p) == {"OPENPROXY_ADMIN_TOKEN": ""}

    def test_env_wins_over_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**真实环境变量压过 ``.env``** —— 这是与 python-dotenv 相反的那条。"""
        monkeypatch.chdir(tmp_path)
        self._write(tmp_path, "OPENPROXY_PORT=1111\nOPENPROXY_HOST=1.1.1.1\n")
        monkeypatch.setenv("OPENPROXY_PORT", "2222")
        merged = merged_env()
        assert merged["OPENPROXY_PORT"] == "2222", "环境变量必须赢"
        assert merged["OPENPROXY_HOST"] == "1.1.1.1", "文件里独有的键要补进来"

    def test_load_settings_reads_dotenv_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``load_settings()`` 不传参时会读 ``.env``（相对当前工作目录）。"""
        monkeypatch.chdir(tmp_path)
        self._write(tmp_path, (
            "OPENPROXY_PORT=10490\n"
            "OPENPROXY_OPENCODE_BASE=http://127.0.0.1:10490\n"
        ))
        # 清掉可能污染的真实环境变量
        for key in ("OPENPROXY_PORT", "OPENPROXY_OPENCODE_BASE"):
            monkeypatch.delenv(key, raising=False)
        s = load_settings()
        assert s.port == 10490
        assert s.opencode_base == "http://127.0.0.1:10490"

    def test_explicit_env_argument_skips_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式传 ``env=`` 就**完全不碰** ``.env``（测试隔离，R15）。

        这条测试第一版**结构上无法失败**（review 的 A-3 发现）：它只断言
        「显式 env 里的键生效」，而那个键在两种实现下都生效。真正要钉的是
        **「文件里那些显式 env 没提到的键不该被带进来」**。
        """
        monkeypatch.chdir(tmp_path)
        self._write(tmp_path, (
            "OPENPROXY_PORT=10490\n"
            "OPENPROXY_HOST=10.0.0.9\n"        # 显式 env 里没有这个键
        ))
        s = load_settings({"OPENPROXY_PORT": "7000"})
        assert s.port == 7000, "显式传入的映射应决定结果"
        assert s.host != "10.0.0.9", (
            "显式传 env= 时不该把 .env 的键带进来 —— "
            "否则测试会被开发者本机的 .env 污染"
        )
        assert s.host == Settings().host, "未提到的键应保持内置默认值"

    def test_template_is_valid_and_documented(self) -> None:
        """``.env.template`` 必须能被解析，且每个键都在 ``Settings`` 里。

        这条防的是「模板里写了一个键名拼错」—— 而那种错只有部署时才暴露。
        """
        template = Path(__file__).resolve().parent.parent / ".env.template"
        assert template.is_file(), ".env.template 不存在"
        parsed = load_dotenv(template)
        # 模板里全是注释 + 被注释掉的示例，所以解析结果应该很少
        assert "OPENPROXY_HOST" in parsed, "模板至少要有一行是生效的示例"
        for key in parsed:
            name = key.removeprefix("OPENPROXY_").lower()
            assert hasattr(Settings(), name), (
                f"模板里的 {key} 在 Settings 上没有对应字段"
            )


def test_repr_never_leaks_secrets() -> None:
    """**``repr(Settings)`` 不能包含任何密钥**（review 的 B-3，P1）。

    这不是理论风险：实测 2026-10-05 做变异测试时，一个失败的 pytest 断言
    把本机 ``.env`` 里的**真实 opencode 密码**原样打印进了 CI 输出 ——
    因为 dataclass 默认 ``repr=True``，而 ``repr`` 会出现在断言失败信息、
    ``logger.exception``、调试器变量展开里。

    加遮罩只保护了 ``--print-config`` 与 ``public_dict()`` 两条路，
    ``repr`` 是**最容易被漏掉的那条**。
    """
    blob = repr(Settings(
        upstream_key="sk-UPSTREAM-SECRET",
        admin_token="admin-SECRET",
        opencode_password="oc-SECRET",
    ))
    for secret in ("sk-UPSTREAM-SECRET", "admin-SECRET", "oc-SECRET"):
        assert secret not in blob, f"repr 泄漏了{secret[:12]}…：{blob}"
    # 非密钥字段仍要在 repr 里 —— 否则这个断言可以用「把 repr 清空」骗过
    assert "8787" in blob, "非密钥字段不该被一起隐藏"


class TestSecretMasking:
    """``--print-config`` 的遮罩必须覆盖**全部三个**密钥（review 的 B-2）。

    ``opencode_password`` 最容易漏：它的 ``None`` 会被
    :func:`~openproxy.service.opencode_client.discover_password` 自动填上，
    所以**即使 ``.env`` 里没写密码，这个字段也经常是已设置状态**。
    而这个命令的用途恰恰是「贴给别人看」。
    """

    def test_print_config_masks_all_three(self) -> None:
        from openproxy.__main__ import _safe_config_dump

        out = _safe_config_dump(Settings(
            upstream_key="sk-UPSTREAM-SECRET",
            admin_token="admin-SECRET",
            opencode_password="oc-SECRET",
        ))
        for secret in ("sk-UPSTREAM-SECRET", "admin-SECRET", "oc-SECRET"):
            assert secret not in out, f"--print-config 泄漏了{secret[:12]}…"
        assert "已设置" in out, "应显示「已设置」而不是值本身"

    def test_print_config_shows_unset_as_such(self) -> None:
        from openproxy.__main__ import _safe_config_dump

        out = _safe_config_dump(Settings())
        assert out.count("<未设置>") >= 1, "未设置的密钥应显式标注"


class TestOverlays:
    def test_patch_only_touches_given_fields(self) -> None:
        base = Overlays(require_key=True, retain_days=10)
        after = base.patch(retain_days=20)
        assert after.require_key is True
        assert after.retain_days == 20

    def test_patch_rejects_unknown_field(self) -> None:
        with pytest.raises(ConfigError, match="未知的覆盖项"):
            Overlays().patch(nope=1)

    def test_validated_enforces_upstream_scheme(self) -> None:
        with pytest.raises(ConfigError, match="upstream_base"):
            Overlays(upstream_base="ws://x").validated()

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_blank_upstream_base_clears_the_overlay(self, blank: str) -> None:
        """留空 = 取消覆盖、回到环境变量基线（控制台的提示就是这么写的）。

        在此之前 ``""`` 会被 URL 校验拒成 422，于是覆盖层一旦设上就再没有任何值
        能清掉它 —— 只能「恢复默认」，而那会连带清掉另外五个字段。
        """
        assert Overlays(upstream_base=blank).validated().upstream_base is None

    def test_upstream_base_is_stripped_like_the_env_path(self) -> None:
        """带首尾空白也照收，但存进去的必须是干净的值。

        不 strip 的话 ``"http://x/zen  "`` 会一路带到出站 URL 上变成
        ``/zen%20%20/v1/...``，症状是「上游突然 404」。
        """
        cleaned = Overlays(upstream_base="  http://x/zen  ").validated()
        assert cleaned.upstream_base == "http://x/zen"

    def test_env_and_overlay_agree_on_whitespace(self) -> None:
        """两条入口对空白的处理必须一致。"""
        assert load_settings({"OPENPROXY_UPSTREAM_BASE": " http://x/zen "}).upstream_base == (
            "http://x/zen"
        )

    def test_validated_enforces_retain_range(self) -> None:
        with pytest.raises(ConfigError, match="retain_days"):
            Overlays(retain_days=0).validated()
        with pytest.raises(ConfigError, match="retain_days"):
            Overlays(retain_days=3651).validated()

    def test_validated_allows_zero_global_quota(self) -> None:
        assert Overlays(daily_token_quota=0).validated().daily_token_quota == 0


class TestRuntimeConfig:
    def test_compose_without_overlays_uses_baseline(self) -> None:
        base = Settings(require_key=False, retain_days=30)
        rc = RuntimeConfig.compose(base, None)
        assert rc.require_key is False
        assert rc.retain_days == 30
        assert rc.overlays == Overlays()

    def test_overlays_win(self) -> None:
        base = Settings(require_key=False, retain_days=30)
        rc = RuntimeConfig.compose(base, Overlays(require_key=True, retain_days=7))
        assert rc.require_key is True
        assert rc.retain_days == 7

    def test_upstream_base_trailing_slash_normalised(self) -> None:
        rc = RuntimeConfig.compose(Settings(), Overlays(upstream_base="http://x/zen/"))
        assert rc.upstream_base == "http://x/zen"

    def test_public_dict_never_leaks_secrets(self) -> None:
        rc = RuntimeConfig.compose(Settings(upstream_key="sk-S3CRET", admin_token="ADM-9f2aZ"))
        blob = repr(rc.public_dict())
        assert "sk-S3CRET" not in blob
        assert "ADM-9f2aZ" not in blob
        assert rc.public_dict()["upstream_authenticated"] is True
        assert rc.public_dict()["admin_protected"] is True

    def test_public_dict_flags_absent_secrets(self) -> None:
        rc = RuntimeConfig.compose(Settings())
        assert rc.public_dict()["upstream_authenticated"] is False
        assert rc.public_dict()["admin_protected"] is False
