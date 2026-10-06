"""opencode 进程守护 + 可用性闸门。

这些是「跑一次就没人再看」的代码：启动时的判断错了，整套件照样绿，
而用户看到的是「启动成功了但每个请求都 504」。所以这里逐条钉住。
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from openproxy.service.opencode_supervisor import (
    Health,
    OpencodeUnavailable,
    Started,
    check_health,
    find_cli,
    is_listening,
    local_endpoint,
    read_password,
    resolve_password,
)

# ------------------------------------------------------------ 测试替身 ----


def _fake_home(path: Path) -> Any:
    """``Path.home`` 的替身。

    **返回「一个返回 Path 的函数」**，而不是一个自定义类 ——
    ``find_cli`` 里写的是 ``Path.home() / "..."``：若``Path.home`` 返回
    一个类（而不是 Path 实例），这行就变成「类 / 字符串」，
    报出来的错是 ``unsupported operand type(s) for /:'_Home' and 'str'``
    —— 与「替身写错了」毫无关联，纯浪费排查时间。

    之前踩过两个坑，都源于让替身「像类」：
    - 返回**类** -> ``_Home / str``报「unsupported operand」
    - 返回**带实例方法的实例** -> ``Path.home()`` 报「object is not callable」
    所以这里就是最朴素的 ``lambda: path``，包一层只为让它有名字
    （ruff 的 ARG005盯的是未使用的 lambda 参数，这里没有）。
    """

    def home() -> Path:
        return path

    return home


def _always_false(*args: Any, **kwargs: Any) -> bool:
    """「没人监听」的替身。用 ``*args`` 收下位置参数而不是写死签名 ——
    被替换的函数签名会变，写死的那版一改就 ``TypeError``。"""
    return False


def _always_true(*args: Any, **kwargs: Any) -> bool:
    """「有人在监听」的替身。"""
    return True


def _boom_spawn(why: str) -> Any:
    """一个「被调用就说明代码走错了分支」的 ``_spawn`` 替身。"""

    async def _spawn(*a: object, **kw: object) -> object:
        raise AssertionError(why)

    return _spawn


def _fake_cli() -> str:
    return "/usr/local/bin/opencode"


def _patch(module: Any, name: str, value: object) -> object:
    """给模块的一个属性打覆盖，返回**原值**以便还原。

    ## 为什么用这个而不是 ``模块.attr = 值``

    直接赋值在 mypy strict 下要写 ``# type: ignore[attr-defined]``
    （这些私有名字不在模块的显式导出里）。而那个 ignore 会在**任何一次
    签名/导出变动后变成「unused」** —— 本项目开了 ``warn_unused_ignores``，
    于是每改一次签名就要重排一次 ignore 的位置。

    ``setattr`` 不需要任何ignore，也让「改了签名就崩」变成不可能。
    """
    old = getattr(module, name)
    setattr(module, name, value)
    return old


def _fake_pids(*pids: int) -> Any:
    """``_pids_on_port`` 的替身。

    **用具名函数而不是 lambda** —— lambda 里那个未使用的 ``port`` 会触发
    ARG005，而它恰是被替换的签名要求的一部分。返回的函数**接受**该参数、
    只是不用它。
    """

    def _pids(port: int) -> list[int]:
        return list(pids)

    return _pids


def _fake_cmdlines(table: dict[int, str]) -> Any:
    """``_cmdline_of`` 的替身：按 pid 查表，没给的返回空串
    （那正是「读不到命令行」的情形）。
    """

    def _cmdline(pid: int) -> str:
        return table.get(pid, "")

    return _cmdline


def _no_cli() -> None:
    return None


class TestLocalEndpoint:
    """只认loopback —— 判定错了会去「托管」别人的服务。"""

    @pytest.mark.parametrize(
        ("base", "expected"),
        [
            ("http://127.0.0.1:4096", ("127.0.0.1", 4096)),
            ("http://localhost:4096", ("127.0.0.1", 4096)),
            ("http://127.0.0.53:8080", ("127.0.0.53", 8080)),
            ("http://[::1]:4096", ("127.0.0.1", 4096)),
            # 非本机 —— 本站不接管它的生命周期
            ("https://opencode.ai/zen", None),
            ("http://192.168.1.10:4096", None),
            # 0.0.0.0 是「本机监听」但不是「可管理的那一个」
            ("http://0.0.0.0:4096", None),
            # 缺端口 —— 无法判断该连哪
            ("http://127.0.0.1", None),
        ],
    )
    def test_recognises_only_manageable_endpoints(
        self, base: str, expected: tuple[str, int] | None
    ) -> None:
        assert local_endpoint(base) == expected

    def test_bare_host_port_is_accepted(self) -> None:
        """``127.0.0.1:4096``（无 scheme）也要认 —— 环境变量里常见。"""
        assert local_endpoint("127.0.0.1:4096") == ("127.0.0.1", 4096)


class TestIsListening:
    def test_closed_port_is_not_listening(self) -> None:
        # 借一个确定没人用的高位端口
        assert not is_listening("127.0.0.1", 9, timeout=0.2)

    def test_real_listener_is_detected(self) -> None:
        """真起一个 socket 服务—— 不实测就会写出「以为」而已的断言。"""
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            assert is_listening("127.0.0.1", port, timeout=1.0)


class TestPasswordResolution:
    def test_explicit_wins(self) -> None:
        assert resolve_password("explicit") == "explicit"

    def test_falls_back_to_the_file(self, tmp_path: Path) -> None:
        """留空时自动读 ``~/.config/opencode/service.json``。

        **这条是刚需而不是便利**：实测 ``opencode serve`` 每次重启生成的
        密码都不同，手填的必然在某次重启后过期（而症状是 401 死循环）。
        """
        svc = tmp_path / "service.json"
        svc.write_text(json.dumps({"password": "from-file"}))
        assert read_password(svc) == "from-file"

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        """读不到就返回 ``None`` —— 让上层回落到显式配置，而不是崩。"""
        assert read_password(tmp_path / "不存在.json") is None

    def test_malformed_file_is_not_an_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "坏.json"
        bad.write_text("{不是 json")
        assert read_password(bad) is None

    def test_bom_is_tolerated(self, tmp_path: Path) -> None:
        """带 BOM 的 service.json 也要读得到 —— 与 ``.env`` 同理。

        不剥 BOM 时 ``json.loads`` 抛「Expecting value」，而那被 ``except``
        吞掉之后的症状是**「密码读不到」**而不是「文件坏了」—— 极难联想到
        真正的成因。
        """
        svc = tmp_path / "service.json"
        svc.write_bytes(
            b"\xef\xbb\xbf" + json.dumps({"password": "bom"}).encode()
        )
        assert read_password(svc) == "bom"


class TestHealthSemantics:
    """``Health.models == 0`` **不代表不健康** —— 这条最容易搞错。"""

    def test_empty_data_array_is_healthy(self) -> None:
        """真的 opencode返回 ``{"data": []}`` —— 那是正常的。

        实测 2026-10-06：``GET /api/model`` 对真实服务返回空列表
        （它不列各provider 的模型）。若把「空」当成「坏」，
        每次启动都会误判成不可用。
        """

        async def fake_get(*a: Any, **kw: Any) -> Any:
            class R:
                status_code = 200

                @staticmethod
                def json() -> dict[str, Any]:
                    return {"location": {"directory": "/tmp"}, "data": []}

                text = '{"data": []}'

            return R()

        result = asyncio.run(
            _check_with(fake_get, "http://127.0.0.1:1", "pw")
        )
        assert result.ok, f"空 data 数组应算健康，实际 {result.reason}"

    def test_401_explains_the_password(self) -> None:
        async def fake_get(*a: Any, **kw: Any) -> Any:
            class R:
                status_code = 401
                text = '{"_tag":"UnauthorizedError"}'

                @staticmethod
                def json() -> dict[str, Any]:
                    raise ValueError("不是 json")

            return R()

        result = asyncio.run(
            _check_with(fake_get, "http://127.0.0.1:1", "bad")
        )
        assert not result.ok
        assert "密码不对" in result.reason, (
            f"401 的提示要说清是密码问题：{result.reason}"
        )

    def test_connection_error_is_unhealthy(self) -> None:
        async def boom(*a: Any, **kw: Any) -> Any:
            raise OSError("Connection refused")

        result = asyncio.run(
            _check_with(boom, "http://127.0.0.1:1", "pw")
        )
        assert not result.ok
        assert "Connection refused" in result.reason

    def test_missing_data_key_is_unhealthy(self) -> None:
        """``data`` 键**不存在**是真的形状不对（与「存在但为空」不同）。"""

        async def fake_get(*a: Any, **kw: Any) -> Any:
            class R:
                status_code = 200
                text = '{"location": {}}'

                @staticmethod
                def json() -> dict[str, Any]:
                    return {"location": {"directory": "/tmp"}}

            return R()

        result = asyncio.run(
            _check_with(fake_get, "http://127.0.0.1:1", "pw")
        )
        assert not result.ok
        assert "data" in result.reason


async def _check_with(fake_get: Any, base: str, pw: str) -> Health:
    """让 :func:`check_health` 用假的 ``client.get``。

    走真实 httpx 的构造与异常处理，只把「发出去的那一下」换掉 ——
    那样才能同时验到「404 算不健康」和「空 data 算健康」这两条。
    """
    import openproxy.service.opencode_supervisor as sup

    class FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, *a: Any, **kw: Any) -> Any:
            return await fake_get(*a, **kw)

    original = sup.httpx.AsyncClient  # type: ignore[attr-defined]
    # 换掉 client 的**构造**而不是 ``.get`` —— 那样才同时验到
    # 「404 算不健康」和「空 data 算健康」这两条（它们都在 get 之后）。
    sup.httpx.AsyncClient = FakeClient  # type: ignore[attr-defined]
    try:
        return await check_health(base, pw)
    finally:
        sup.httpx.AsyncClient = original  # type: ignore[attr-defined]


class TestFindCli:
    def test_path_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """先查 PATH —— 那才是「用户装的那个」。"""
        monkeypatch.setattr(
            "openproxy.service.opencode_supervisor.shutil.which",
            lambda _: "/usr/local/bin/opencode",
        )
        assert find_cli() == "/usr/local/bin/opencode"

    def test_falls_back_to_the_desktop_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PATH 里没有就找桌面版自带的 CLI。

        桌面版的路径带**版本号**（实测 ``cli/2.0.23/opencode-cli``），
        下次升级就变 —— 所以只能glob，不能硬编路径。
        """
        monkeypatch.setattr(
            "openproxy.service.opencode_supervisor.shutil.which",
            lambda _: None,
        )
        monkeypatch.setattr(Path, "home", _fake_home(tmp_path))
        cli_dir = (
            tmp_path / "Library/Application Support/ai.opencode.desktop/cli/2.0.23"
        )
        cli_dir.mkdir(parents=True)
        (cli_dir / "opencode-cli").write_text("#!/bin/sh\n")
        assert find_cli() == str(cli_dir / "opencode-cli")

    def test_returns_none_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "openproxy.service.opencode_supervisor.shutil.which",
            lambda _: None,
        )
        monkeypatch.setattr(Path, "home", _fake_home(tmp_path))
        assert find_cli() is None


class TestPasswordIsWrittenBack:
    """自动拉起后，密码**必须回写到 Settings** —— 否则启动一切正常、
    每个用户请求却401。

    ## 这条是本轮实测踩出来的

    实测 2026-10-06：``_spawn`` 拿到了新起的 opencode 的密码、健康检查
    也通过了（用的是新密码），但**代理层**走的是
    ``RuntimeConfig.opencode_password`` —— 那是 ``Settings`` 的快照，
    里面还是 ``service.json`` 里的旧密码。

    结果：启动日志一切正常（「opencode 已就绪」「openproxy 已启动」），
    第一个用户请求 401。**启动检查通过 ≠ 转发能用** ——
    两处密码来源不一致时，检查和转发会各用各的。
    """

    def test_started_password_replaces_the_stale_one(self) -> None:
        import argparse
        import dataclasses as dc

        from openproxy.__main__ import build_parser, ensure_opencode_ready
        from openproxy.config import Settings
        from openproxy.service.opencode_supervisor import Started

        args = build_parser().parse_args([])
        settings = Settings(opencode_password="STALE_FROM_OTHER_PROCESS")

        fake_proc = object()  # 只用到类型标注，不会真被terminate
        started = Started(
            process=fake_proc,  # type: ignore[arg-type]
            base="http://127.0.0.1:10490",
            password="FRESH_FROM_STDOUT",
            model_count=0,
        )

        import openproxy.__main__ as m

        async def fake_ensure(*a: object, **kw: object) -> Started:
            return started

        # 直接改 ``__main__`` 的模块属性 —— 绕开「从它 import 一个
        # 未导出名字」的类型检查（``ensure_opencode`` 是从 supervisor
        # 转手import 的，不在 ``__all__`` 里，而为测试加导出反而会
        # 让mypy 报「该名字被遮蔽」）。
        m.__dict__["ensure_opencode"] = fake_ensure
        try:
            out = ensure_opencode_ready(settings, args)
        finally:
            m.__dict__.pop("ensure_opencode", None)

        assert out.opencode_password == "FRESH_FROM_STDOUT", (
            f"密码没回写，转发层会继续用 {out.opencode_password!r} -> 每个请求 401"
        )
        # 确认真的改了对象而不是就地改（Settings 是 frozen）
        assert settings.opencode_password == "STALE_FROM_OTHER_PROCESS"
        assert out is not settings
        del argparse, dc

    def test_skip_check_leaves_settings_untouched(self) -> None:
        """``--no-opencode-check`` 时不该动配置 —— 没有新密码可写。"""
        import argparse

        from openproxy.__main__ import build_parser, ensure_opencode_ready
        from openproxy.config import Settings

        args = build_parser().parse_args(["--no-opencode-check"])
        settings = Settings(opencode_password="whatever")
        out = ensure_opencode_ready(settings, args)
        assert out.opencode_password == "whatever"
        del argparse


    def test_handles_existing_listener_that_is_usable(self) -> None:
        """**健康就直接用** —— 绝不能顺手杀掉重启（那会断掉别人的连接）。"""
        import openproxy.service.opencode_supervisor as sup

        async def healthy(*a: object, **kw: object) -> Health:
            return Health(True, "", 0)

        old_listen = _patch(sup, "is_listening", _always_true)
        old_health = _patch(sup, "check_health", healthy)
        old_spawn = _patch(
            sup, "_spawn",
            _boom_spawn("不该被调用 —— 健康的进程不该被接管"),
        )
        try:
            got = asyncio.run(
                sup._handle_existing_listener(
                    "http://127.0.0.1:10490", "127.0.0.1", 10490, "pw", 0.2, True,
                )
            )
        finally:
            _patch(sup, "is_listening", old_listen)
            _patch(sup, "check_health", old_health)
            _patch(sup, "_spawn", old_spawn)
        assert got is not None and got.ok

    def test_does_not_take_over_a_healthy_process(self) -> None:
        """**健康就直接用，绝不接管**。

        这条与下一条构成分叉：那条是「不健康 -> 接管」，这条是
        「健康 -> 立刻返回」。少了它，一个「无论健康与否都接管」的实现
        也能通过 —— 而那会**断掉别人正在用的连接**。
        """
        import openproxy.service.opencode_supervisor as sup

        async def healthy(*a: object, **kw: object) -> Health:
            # reason 要**非空**：``_handle_existing_listener`` 里那个
            # 「等一下就好」的重试循环是 ``if "密码不对" not in health.reason``
            # 才进的 —— reason 为空时它也会进，而循环内还有第二处
            # ``if health.ok: return health``。若 reason 为空，两处都会返回，
            # 变异掉第一处就看不出来（这正是本测试第一版钉不住的原因）。
            return Health(True, "探测用（刻意非空）", 0)

        def boom(host: str, port: int) -> bool:
            raise AssertionError("健康的服务不该被接管 —— 那会断掉别人的连接")

        old_health = _patch(sup, "check_health", healthy)
        old_take = _patch(sup, "_take_over_port", boom)
        try:
            got = asyncio.run(
                sup._handle_existing_listener(
                    "http://127.0.0.1:10490", "127.0.0.1", 10490,
                    "pw", 0.2, True,
                )
            )
        finally:
            _patch(sup, "check_health", old_health)
            _patch(sup, "_take_over_port", old_take)
        assert got is not None and got.ok, "健康时应直接返回 Health"

    def test_takes_over_when_password_is_wrong(self) -> None:
        """密码不对 -> **接管**（杀掉那个拿不到密码的进程，起自己的）。"""
        import openproxy.service.opencode_supervisor as sup

        async def bad_auth(*a: object, **kw: object) -> Health:
            return Health(False, "密码不对（HTTP 401）")

        calls: list[str] = []

        def fake_take_over(host: str, port: int) -> bool:
            calls.append(f"take_over:{port}")
            return True

        def fake_listen(*a: object, **kw: object) -> bool:
            # 第一次：接管前「有东西在听」；之后（起新的之后）要返回 False
            return not calls

        old_health = _patch(sup, "check_health", bad_auth)
        old_take = _patch(sup, "_take_over_port", fake_take_over)
        old_listen = _patch(sup, "is_listening", fake_listen)
        try:
            got = asyncio.run(
                sup._handle_existing_listener(
                    "http://127.0.0.1:10490", "127.0.0.1", 10490, "pw", 0.2, True,
                )
            )
        finally:
            _patch(sup, "check_health", old_health)
            _patch(sup, "_take_over_port", old_take)
            _patch(sup, "is_listening", old_listen)
        assert calls == ["take_over:10490"], f"应该接管一次，实际 {calls}"
        assert got is None, "接管后应返回 None，让调用方接着起自己的"

    def test_does_not_take_over_when_autostart_is_off(self) -> None:
        """``--no-opencode-autostart`` 时**不接管**（用户明说了别动）。"""
        import openproxy.service.opencode_supervisor as sup

        async def bad_auth(*a: object, **kw: object) -> Health:
            return Health(False, "密码不对（HTTP 401）")

        def boom(host: str, port: int) -> bool:
            raise AssertionError("不该被调用 —— 自动启动已关")

        old_health = _patch(sup, "check_health", bad_auth)
        old_take = _patch(sup, "_take_over_port", boom)
        old_listen = _patch(sup, "is_listening", _always_true)
        try:
            with pytest.raises(OpencodeUnavailable) as exc:
                asyncio.run(
                    sup._handle_existing_listener(
                        "http://127.0.0.1:10490", "127.0.0.1", 10490,
                        "pw", 0.2, False,
                    )
                )
        finally:
            _patch(sup, "check_health", old_health)
            _patch(sup, "_take_over_port", old_take)
            _patch(sup, "is_listening", old_listen)
        assert "no-opencode-autostart" in exc.value.detail, (
            f"要说清怎么打开接管：{exc.value.detail}"
        )


class TestTakeOverPort:
    """**接管**占着端口的进程 —— 而不是让用户自己去杀。

    ## 为什么必须接管

    实测 2026-10-06（用户真实踩到）：10490 上有个之前手工起的
    ``opencode-cli``，而 ``~/.config/opencode/service.json`` 是**桌面版独占**
    的（只有它写那个文件）—— 里面那个密码对 CLI 起的服务**无效**
    （实测同一个密码：桌面版 200、CLI 401）。而 CLI 把自己的密码只打在
    它自己的 stdout 上，那个 stdout 随进程退出就消失了。

    也就是说**那个进程的密码在物理上已经拿不到了**。「请手工处理」是句空话
    —— 用户能做的只有杀掉它或放弃这条路。所以本站自己接管。
    """

    def test_kills_only_opencode_processes(self) -> None:
        """**只杀 opencode** —— 按端口无差别 kill 会误伤别人的服务。"""
        killed: list[int] = []

        import openproxy.service.opencode_supervisor as sup

        def fake_run(argv: list[str], **kw: object) -> object:
            if argv[0] == "kill" and int(argv[1]) == 111:
                killed.append(int(argv[1]))
            return _FakeCompleted()

        # 只换``subprocess.run``（不是整个 subprocess 模块）—— 代码里
        # 用的是 ``subprocess.run(...)``，换掉整个模块会让它变成
        # ``_FakeSubprocess.run`` 那种形状而报 TypeError。
        old_pids = _patch(sup, "_pids_on_port", _fake_pids(111, 222))
        old_cmd = _patch(
            sup, "_cmdline_of",
            _fake_cmdlines({111: "opencode-cli", 222: "postgres"}),
        )
        old_run = _patch(sup.subprocess, "run", fake_run)
        try:
            ok = sup._take_over_port("127.0.0.1", 10490)
        finally:
            _patch(sup, "_pids_on_port", old_pids)
            _patch(sup, "_cmdline_of", old_cmd)
            _patch(sup.subprocess, "run", old_run)
        assert killed == [111], f"只该杀 opencode，实际杀了 {killed}"
        assert not ok, "第二个不是 opencode -> 必须放弃并返回 False"

    def test_refuses_when_cmdline_is_unreadable(self) -> None:
        """**拿不到命令行就放弃** —— 把「不知道」当成「不是」会误杀。

        反过来同样坏：``ps`` 在受限环境里可能被拒绝执行（实测
        ``Operation not permitted``），那时若按「不是」处理，用户就会看到
        一个「非 opencode 进程」的错误 —— 而那个进程**就是** opencode。
        """
        import openproxy.service.opencode_supervisor as sup

        old_pids = _patch(sup, "_pids_on_port", _fake_pids(333))
        old_cmd = _patch(sup, "_cmdline_of", _fake_cmdlines({}))
        try:
            assert not sup._take_over_port("127.0.0.1", 10490)
        finally:
            _patch(sup, "_pids_on_port", old_pids)
            _patch(sup, "_cmdline_of", old_cmd)

    def test_no_pids_means_already_free(self) -> None:
        """端口上本来就没东西 -> 算「已腾空」。"""
        import openproxy.service.opencode_supervisor as sup

        old_pids = _patch(sup, "_pids_on_port", _fake_pids())
        old_listen = _patch(sup, "is_listening", _always_false)
        try:
            assert sup._take_over_port("127.0.0.1", 10490)
        finally:
            _patch(sup, "_pids_on_port", old_pids)
            _patch(sup, "is_listening", old_listen)


class _FakeCompleted:
    """``subprocess.run`` 的最小替身。"""

    stdout = ""
    stderr = b""
    returncode = 0


class TestCmdlineOf:
    """``_cmdline_of`` **绝不能抛异常** —— 抛出去会让整个启动崩掉。

    实测 2026-10-06：受限环境里 ``ps -p<pid>`` 返回
    ``Operation not permitted``，上一版没捕那个异常，用户看到的是一段
    Python traceback 而不是「那个服务密码不对」。
    """

    def test_uses_lsof_not_ps(self) -> None:
        """用 ``lsof`` 而不是 ``ps`` —— 实测 ``ps`` 在受限环境里被拒。"""
        import openproxy.service.opencode_supervisor as sup

        seen: list[list[str]] = []

        class R:
            stdout = (
                "COMMAND     PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
                "opencode. 451261 user   11u  IPv4 0x1 0t0  TCP 127.0.0.1:10490\n"
            )
            stderr = b""
            returncode = 0

        def fake_run(argv: list[str], **kw: object) -> object:
            seen.append(argv)
            return R()

        orig = _patch(sup.subprocess, "run", fake_run)
        try:
            got = sup._cmdline_of(45126)
        finally:
            _patch(sup.subprocess, "run", orig)
        assert "opencode" in got, f"没认出是 opencode：{got!r}"
        assert seen and seen[0][0] == "lsof", (
            f"主路径应该是 lsof（ps 在受限环境里被拒），实际是 {seen}"
        )

    def test_permission_error_degrades_to_empty(self) -> None:
        """``lsof`` 也被拒时返回空串，**不抛**。"""
        import openproxy.service.opencode_supervisor as sup

        def fake_run(argv: list[str], **kw: object) -> object:
            raise PermissionError(1, "Operation not permitted")

        orig = _patch(sup.subprocess, "run", fake_run)
        try:
            assert sup._cmdline_of(111) == ""
        finally:
            _patch(sup.subprocess, "run", orig)


class TestSpawnPasswordOrder:
    """**必须读完两行**才返回 —— 这条踩过两次。"""

    def test_spawn_returns_the_password_not_the_stale_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """直接验 :func:`_spawn` —— 上面那条只验了「输出是两行」。

        这里必须真的跑一遍 ``_spawn`` 并检查它返回的密码，因为
        「只读一行就return」那个 bug 的症状是：返回了 ``service.json`` 里
        **另一个进程**的密码，而断言「输出形状」完全测不出来。
        """
        import sys

        from openproxy.service.opencode_supervisor import _spawn

        script = tmp_path / "fake_cli.py"
        script.write_text(
            "import time\n"
            "print('server listening on http://127.0.0.1:10490', flush=True)\n"
            "time.sleep(0.3)\n"
            "print('server password THE_REAL_ONE', flush=True)\n"
            "time.sleep(30)\n"
        )
        # 让 service.json 指向一个**内容不同**的密码 —— 那正是真实场景：
        # 桌面版写的 service.json 与 CLI 起的进程密码不同（实测不相等）。
        svc = tmp_path / "service.json"
        svc.write_text(json.dumps({"password": "STALE_FROM_OTHER_PROCESS"}))

        proc, password = asyncio.run(
            _spawn(sys.executable, "127.0.0.1", 10490,
                   fake_cli=[sys.executable, str(script)])
        )
        try:
            assert password == "THE_REAL_ONE", (
                f"拿到了 {password!r} 而不是 stdout 上那个 —— "
                "若它等于 service.json 里那个值，说明只读了第一行就 return"
            )
            assert password != "STALE_FROM_OTHER_PROCESS"
        finally:
            proc.kill()
            if proc.stdout is not None:
                proc.stdout.close()
            proc.wait(timeout=5)

    def test_password_line_comes_after_listening(
        self, tmp_path: Path
    ) -> None:
        """实测真实 ``serve`` 的打印顺序是::

            server listening on http://...
            server password XXXXX

        而 ``readline`` 是阻塞的：看到 listening 就 return 的话，密码还没读进
        变量 -> 退回 ``service.json`` —— 而**那个文件里是另一个进程
        （桌面版）的密码**（实测两者不相等）-> 401 死循环到超时。

        这条钉住那个**顺序**本身（用真opencode 的输出形状），
        上面那条钉住「所以必须读两行」这个行为。
        """
        import subprocess
        import sys

        script = tmp_path / "fake_cli.py"
        script.write_text(
            "import time\n"
            "print('server listening on http://127.0.0.1:10490', flush=True)\n"
            "time.sleep(0.2)\n"
            "print('server password THE_REAL_ONE', flush=True)\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            assert proc.stdout is not None
            first = proc.stdout.readline().decode().strip()
            second = proc.stdout.readline().decode().strip()
            assert "listening" in first, first
            assert "password" in second, (
                f"第二行才是密码 —— 只读一行会漏掉它：{second!r}"
            )
            assert second.rsplit(" ", 1)[-1] == "THE_REAL_ONE"
        finally:
            proc.kill()
            # **必须 wait 且关掉 stdout** —— 否则 3.14 下会抛
            # ResourceWarning（unclosed file）+ Popen.__del__ 的
            # PytestUnraisableExceptionWarning，而本项目
            # ``filterwarnings = error`` 把它们都变成失败。
            if proc.stdout is not None:
                proc.stdout.close()
            proc.wait(timeout=5)


class TestEnsureOpencodeExternal:
    """非本机地址：**只检查不托管**。"""

    def test_external_base_is_only_health_checked(self) -> None:
        calls: list[str] = []

        async def go() -> Health | Started:
            import openproxy.service.opencode_supervisor as s

            orig_health, orig_spawn = s.check_health, s._spawn

            async def ch(base: str, pw: str | None, timeout: float = 3.0) -> Health:
                calls.append(base)
                return Health(True, "", 0)

            async def sp(*a: Any, **kw: Any) -> Any:
                calls.append("SPAWNED")  # 不该被调用
                raise AssertionError("外部地址不该起进程")

            s.check_health, s._spawn = ch, sp  # type: ignore[assignment]
            try:
                return await s.ensure_opencode(
                    "https://example.com/zen", "pw", auto_start=True,
                )
            finally:
                s.check_health, s._spawn = orig_health, orig_spawn

        result = asyncio.run(go())
        assert isinstance(result, Health)
        assert calls == ["https://example.com/zen"], (
            f"外部地址只该做健康检查，不该起进程：{calls}"
        )


class TestUnavailableMessage:
    """每种失败都要带「接下来该做什么」。

    **断言的是 ``exc.detail`` 而不是 ``str(exc)``** —— ``detail`` 是
    ``__main__`` 真正打印给用户看的那一段（``→ 装一个（brew install…）``），
    而 ``str(exc)`` 只是标题。测错字段的话，这组测试会给出一个虚假的绿灯。
    """

    def test_detail_is_not_empty(self) -> None:
        """只说「失败了」等于把排查成本原样退回给用户。"""
        exc = OpencodeUnavailable("起不来", detail="手动跑一次看它报什么")
        assert exc.detail, "必须给出下一步"

    def test_autostart_disabled_says_how_to_start_it_manually(self) -> None:
        exc = asyncio.run(_ensure_with_no_listener(auto_start=False, port=10490))
        assert "opencode serve --port 10490" in exc.detail, (
            f"要说清怎么手动起：{exc.detail}"
        )

    def test_cli_missing_says_how_to_install(self) -> None:
        exc = asyncio.run(_ensure_with_no_cli())
        assert "brew install opencode" in exc.detail, (
            f"要说清怎么装：{exc.detail}"
        )

    def test_cli_missing_lists_the_escape_hatches(self) -> None:
        """最后要给出「绕过它」的办法 —— 用户不该只能干等。"""
        exc = asyncio.run(_ensure_with_no_cli())
        assert "--no-opencode" in exc.detail, (
            f"要说清怎么绕过：{exc.detail}"
        )


async def _ensure_with_no_listener(
    *, auto_start: bool, port: int
) -> OpencodeUnavailable:
    import openproxy.service.opencode_supervisor as s

    orig_listen, orig_cli = s.is_listening, s.find_cli
    s.is_listening = _always_false
    s.find_cli = _fake_cli
    try:
        await s.ensure_opencode(
            f"http://127.0.0.1:{port}", "pw", auto_start=auto_start,
            health_timeout=0.2,
        )
    except OpencodeUnavailable as exc:
        return exc
    finally:
        s.is_listening, s.find_cli = orig_listen, orig_cli
    raise AssertionError("应该抛 OpencodeUnavailable")


async def _ensure_with_no_cli() -> OpencodeUnavailable:
    import openproxy.service.opencode_supervisor as s

    orig_listen, orig_cli = s.is_listening, s.find_cli
    s.is_listening = _always_false
    s.find_cli = _no_cli
    try:
        await s.ensure_opencode(
            "http://127.0.0.1:10490", "pw", auto_start=True,
            health_timeout=0.2,
        )
    except OpencodeUnavailable as exc:
        return exc
    finally:
        s.is_listening, s.find_cli = orig_listen, orig_cli
    raise AssertionError("应该抛 OpencodeUnavailable")
