"""opencode CLI 的**进程守护 + 可用性闸门**。

## 为什么需要这个

实测 2026-10-06：``opencode_base`` 指向的地址没人监听时，本站的表现是
``504 upstream_unreachable``，而错误信息只能说到「连不上」为止 ——
用户得自己猜是「服务没起」「端口配错」还是「被代理绕了」。

于是把它做成启动前的**硬闸门**：``opencode`` 不可用就不让 ``openproxy`` 起来。
理由很直接 —— 一个转发站如果上游根本没起来，启动它没有任何意义，
而「启动失败 + 一条说清原因的错误」比「起来了但每个请求都 504」有用得多。

## 三种情况

============  ==========================================================
``opencode_base``为空/ 非本机  **不检查** —— 那是用户自己接的外部服务，
                                本站管不了也不该管
本机地址且**已有进程在听**  直接用，不重复起
本机地址且**没人监听**      尝试起一个 CLI；起不来 / 起完仍不健康 → 退出码 2
============  ==========================================================

## 「起完了还不健康」的处理

``serve`` 打印 ``server listening on ...`` 只说明 **socket 绑好了**，
不说明它能应答 —— 实测过「端口在听但 ``GET /api/model`` 一直 502」的窗口期。
所以 :func:`ensure_opencode` 在起进程之后会**轮询等健康**，而不是看到端口就
返回成功。等不到就把它杀掉并报错 —— 留一个半死不活的进程比没有更糟。

## 密码

**默认自动读** ``~/.config/opencode/service.json``（由 ``opencode serve`` 与
桌面版写入），所以 ``opencode serve`` 每次重启换密码也不影响配置 ——
实测那个密码是**每次启动都变**的，手填必然过期。

## 端口从哪来

``opencode_base`` 的**端口**。用同一个数字起 CLI 是刻意的：
让「配置里写的地址」与「实际在听的地址」天然一致，不给它们分叉的机会。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from openproxy.service.opencode_client import discover_password

#: 显式导出 ``subprocess`` —— 测试要替换它的 ``run`` 才能验证「杀进程」
#: 那条路径（而不真的杀东西）。不给它导出的话，测试里读``sup.subprocess``
#: 会被 mypy 判成「不显式导出」（strict 模式 + ``no_implicit_reexport``），
#: 于是测试要么写 ``# type: ignore[attr-defined]``、要么就该用别的方式 ——
#: 而那个 ignore 会在任何一次签名变动后变成「unused」又是一轮返工。
__all__ = ["subprocess"]

logger = logging.getLogger(__name__)

#: 密码文件（``opencode serve`` 与桌面版都会写它）
SERVICE_JSON = Path.home() / ".config/opencode/service.json"

#: 起CLI 之后等健康的超时（秒）。实测冷启动约 1~3秒；给 15 秒余量。
HEALTH_TIMEOUT = 15.0
#: 轮询间隔
HEALTH_INTERVAL = 0.25


class OpencodeUnavailable(RuntimeError):
    """opencode 不可用，且本站无法自己把它拉起来。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class Health:
    """健康检查的结果。"""

    ok: bool
    reason: str = ""
    models: int = 0


def read_password(config_path: Path | None = None) -> str | None:
    """从 ``~/.config/opencode/service.json`` 读密码。

    **直接复用** :func:`openproxy.service.opencode_client.discover_password`
    而不是自己再读一遍 —— 两处各读一次的话，改了路径只改一处，
    剩下那处静默读不到（而症状是 401，很难联想到是「读错了地方」）。

    读不到就返回 ``None`` —— 让调用方回落到显式配置，而不是在这里报错：
    这个文件只在「本机自己起的 opencode」时才有意义。

    ``config_path`` 只给测试用（与 :func:`discover_password` 同一个理由：
    让测试指路径，而不是去monkeypatch ``Path.home``）。
    """
    return discover_password(config_path)


def local_endpoint(base: str) -> tuple[str, int] | None:
    """若 ``base`` 指向本机就返回 ``(host, port)``，否则 ``None``。

    「本机」的判定刻意**只认 loopback**（``127.0.0.0/8`` 与 ``::1``）——
    ``localhost`` 也要认，但要防止一种情况：``host=0.0.0.0`` 的本机服务
    不该被当成「可管理」的那个。
    """
    parsed = urlparse(base if "://" in base else f"http://{base}")
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        return None
    if host in ("localhost", "::1"):
        return "127.0.0.1", port
    if host.startswith("127."):
        return host, port
    return None


def is_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """该地址有没有人在听。

    用 socket 而不是发 HTTP：这里只关心「有没有进程占着端口」，
    而 HTTP 会因为服务半死不活而超时 —— 那不属于这一层的判断。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def check_health(
    base: str, password: str | None, timeout: float = 3.0
) -> Health:
    """问它一句「你能列出模型吗」—— 那是最轻的、且真的走认证的请求。

    实测「端口在听但服务没就绪」的窗口期里，socket 连得上但这个请求会 502，
    所以**只探端口会漏掉它**（而那种状态会让每个用户请求都失败）。

    ## ``models`` 那个数字是 0，别当「有几个模型可用」读

    实测 2026-10-06：真的 opencode 起来之后，``GET /api/model`` 返回
    ``{"data": []}`` —— **它不列各 provider 的模型**（那是 ``/api/provider``
    的职责）。所以 ``Health.models`` 对真实服务永远是 0，
    「0 个模型可用」**不是**「opencode 坏了」。

    可用性只看 ``ok``（= 认证通过 + 响应形状对），不看这个数字。
    """
    headers = {"Content-Type": "application/json"}
    if password:
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 2.0))
        ) as client:
            r = await client.get(f"{base.rstrip('/')}/api/model", headers=headers)
    except Exception as exc:
        return Health(False, f"{type(exc).__name__}: {exc}"[:120])
    if r.status_code >= 400:
        if r.status_code in (401, 403):
            return Health(
                False, f"密码不对（HTTP {r.status_code}）—— opencode 每次"
                "重启都会换密码；本站自己起的那个密码在它的 stdout 上，"
                "留空让它自动读，或加 --no-opencode-check 跳过检查",
            )
        return Health(False, f"HTTP {r.status_code}: {r.text[:80]}")
    try:
        payload = r.json()
    except Exception as exc:
        return Health(False, f"响应不是 JSON：{type(exc).__name__}")
    # ``data`` **可以是空列表而服务完全正常**（见 docstring），所以只查
    # 「键在不在、类型对不对」，不查它空不空 —— 查空会把好服务判成坏的。
    if not isinstance(payload, dict) or not isinstance(
        payload.get("data"), list
    ):
        return Health(False, f"响应里没有 data 数组：{r.text[:80]}")
    return Health(True, "", len(payload["data"]))


def find_cli() -> str | None:
    """找 opencode CLI。

    顺序是刻意的：**先查 ``PATH``**，因为那才是「用户装的那个」；
    桌面版自带的 CLI 路径带版本号（实测 ``cli/2.0.22/opencode-cli``），
    下次升级就变，所以它排在后面且必须用 glob 找。
    """
    found = shutil.which("opencode")
    if found:
        return found
    candidates = sorted(
        (Path.home() / "Library/Application Support/ai.opencode.desktop/cli").glob(
            "*/opencode-cli"
        )
    )
    return str(candidates[-1]) if candidates else None


@dataclass
class Started:
    """起起来之后拿到的信息。"""

    process: subprocess.Popen[bytes]
    base: str
    password: str | None
    model_count: int


def _take_over_port(host: str, port: int) -> bool:
    """杀掉占着 ``host:port`` 的进程，让本站能起自己的。

    ## 为什么需要「接管」而不是「报错让人自己处理」

    实测 2026-10-06（用户遇到）：10490 上有个之前手工起的 ``opencode-cli``，
    而 ``~/.config/opencode/service.json`` 里是**桌面版**的密码（只有桌面版
    写那个文件）—— 那个密码对 CLI 起的服务**无效**（实测同一个密码：
    桌面版 200、CLI 401）。而 CLI 把它自己的密码只打在自己的 stdout 上，
    那个 stdout 随进程退出就消失了。

    也就是说：**那个进程的密码在物理上已经拿不到了**。此时「请手工处理」
    是一句空话 —— 用户能做的只有两件事：杀掉它，或者放弃这条路。
    所以本站自己接管。

    ## 返回值

    ``True`` = 杀掉了且端口已释放；``False`` = 杀不掉（不是当前用户的进程、
    或它被别的程序持有）。**杀不掉时绝不能继续** —— 那样起新的会撞端口。

    只杀 ``opencode`` 相关的进程（按命令行匹配），不做「按端口无差别 kill」——
    后者会误伤恰好用着这个端口的别的服务。
    """
    pids = _pids_on_port(port)
    if not pids:
        return not is_listening(host, port, timeout=1.0)
    for pid in pids:
        # 刻意**先问命令行里有没有 opencode**：没有就别动它。
        # 按端口无差别 kill 会误伤恰好用着这个端口的别的服务。
        #
        # ``_cmdline_of`` 内部已处理 macOS 无 ``/proc``、``ps`` 被拒执行等情况，
        # 拿不到时返回空串 —— **空串按「不敢动」处理**（而不是按「不是
        # opencode」处理）：两者都不杀，但消息必须说清是哪一种。
        cmdline = _cmdline_of(pid)
        if "opencode" not in cmdline:
            logger.warning(
                "端口 %d 上有 pid=%d，但读不到它的命令行里有 opencode"
                "（读到的是 %r）—— 不动它。%s",
                port, pid, cmdline[:80],
                "若是权限所致，可用 --no-opencode-autostart 跳过接管，"
                "或手工 kill 后重试。" if not cmdline else "",
            )
            return False
        try:
            subprocess.run(["kill", str(pid)], check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            logger.warning("杀不掉 pid=%d：%s", pid, exc.stderr.decode()[:80])
            return False
    # 给它一点时间真正退出（kill 是异步的）
    for _ in range(20):
        if not is_listening(host, port, timeout=0.2):
            return True
        time.sleep(0.1)
    return False


def _pids_on_port(port: int) -> list[int]:
    """列出监听 ``host:port`` 的 pid。**只取 LISTEN 的**。

    只取 LISTEN 很关键：同一个进程与别的机器/容器通信时会建立一堆
    ESTABLISHED 连接，把它们也算进来会误杀。
    """
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, check=False,
        ).stdout
    except FileNotFoundError:  # pragma: no cover — lsof 在 macOS/Linux 都有
        return []
    pids: list[int] = []
    for line in out.split():
        if line.isdigit():
            pids.append(int(line))
    return pids


def _cmdline_of(pid: int) -> str:
    """读某个 pid 的**可执行文件名**（能认出「是不是 opencode」就够）。

    ## 为什么用 ``lsof`` 而不是 ``ps``

    实测 2026-10-06（受限环境）：``ps -p<pid>`` 返回
    ``Operation not permitted``（macOS 沙箱对 ``ps`` 单独设了限制），
    而 **``lsof`` 能正常工作** —— 而 ``lsof -p <pid>`` 的第一列就是
    ``COMMAND``（可执行文件名，含 ``opencode``）。

    上一版用 ``ps`` 时整个启动直接崩了（``PermissionError`` 穿透到
    ``SystemExit`` 之外，用户看到的是一段 Python traceback 而不是
    「那个服务密码不对」）。**启动期的任何探测失败都必须降级成
    「拿不到信息」，绝不能崩。**

    macOS 没有 ``/proc``，所以 Linux 上的 ``/proc/<pid>/cmdline`` 那条快路径
    在这里派不上用场；留着是为了在 Linux 上省一次 ``ps``。
    """
    try:
        out = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        ).stdout
    except OSError:
        out = ""
    # ``COMMAND`` 列在表头之后的第一列，形如``opencode-cli`` / ``opencode``
    names = {
        line.split(None, 1)[0]
        for line in out.splitlines()[1:]
        if line.strip()
    }
    if names:
        return " ".join(sorted(names))
    proc_file = Path(f"/proc/{pid}/cmdline")
    try:
        if proc_file.exists():
            return proc_file.read_bytes().decode(errors="replace")
    except OSError:
        pass
    return ""


async def _spawn(
    cli: str, host: str, port: int, extra_env: dict[str, str] | None = None,
    *, fake_cli: list[str] | None = None,
) -> tuple[subprocess.Popen[bytes], str]:
    """起CLI 并从 stdout 读到它打印的密码。

    密码**只能从 stdout 拿** —— ``serve`` 每次启动生成一个新的（实测多次确认），
    而 ``service.json`` 里是**另一个进程**（桌面版）的密码（实测两者不相等）。
    所以这里必须先读 stdout 才能拿到密码，不能等 ``service.json``。

    ``fake_cli`` 只给测试用：让测试能塞一个「打印同样两行」的假 CLI，
    从而验出「只读一行就 return」那个 bug —— 否则它会以「密码是
    service.json 里那个」的形式溜过去（那条文件里是**另一个进程**的密码，
    实测两者不相等）。
    """
    env = {**dict(__import__("os").environ), **(extra_env or {})}
    argv = fake_cli or [cli, "serve", "--port", str(port), "--hostname", host]
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        # 脱离当前进程组：openproxy 退出时它不该跟着死（它可能是常驻的），
        # 而 ``start_new_session`` 也让 Ctrl-C 不会波及它。
        start_new_session=True,
    )
    password: str | None = None
    listening = False
    deadline = time.monotonic() + 10.0
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline().decode(errors="replace").strip()
        if not line:
            if proc.poll() is not None:
                raise OpencodeUnavailable(
                    f"opencode 启动就退出了（退出码 {proc.returncode}）",
                    detail="用 `opencode serve --port N --hostname 127.0.0.1` "
                           "手动跑一次看它报什么",
                )
            time.sleep(0.1)
            continue
        if "server password" in line:
            password = line.rsplit(" ", 1)[-1].strip()
        if "listening on" in line:
            listening = True
        # **两行都读到才返回** —— 实测（2026-10-06）打印顺序是
        #   server listening on http://...
        #   server password XXXXX
        # 而 ``readline`` 是阻塞的：看到 listening 就 return 的话，
        # 密码还没读进变量，于是退回 ``service.json`` —— 而**那个文件里
        # 是另一个进程（桌面版）的密码**（实测两者不相等），于是 401。
        if listening and password:
            return proc, password
    proc.kill()
    if listening and not password:
        raise OpencodeUnavailable(
            "opencode 起来了但没打印密码行",
            detail="这个版本的 serve 可能改了输出格式 —— "
                   "请手动起一个并把密码填进 OPENPROXY_OPENCODE_PASSWORD",
        )
    raise OpencodeUnavailable(
        f"opencode 起了但 {HEALTH_TIMEOUT:.0f}s 内没打印「server listening」",
        detail="手动跑一次 `opencode serve --port N` 看它卡在哪",
    )


async def _handle_existing_listener(
    base: str, host: str, port: int, password: str | None,
    health_timeout: float, auto_start: bool,
) -> Health | None:
    """端口上已经有东西在听 —— 决定「能不能用」或「要不要接管」。

    返回 :class:`Health` 表示可以直接用；返回 ``None`` 表示**已把端口腾空**，
    调用方应当接着起自己的。

    ## 为什么需要「接管」而不是报错让人自己处理

    实测 2026-10-06（用户遇到）：10490 上有个之前手工起的``opencode-cli``，
    而 ``~/.config/opencode/service.json`` 是**桌面版独占**的（只有它写那个
    文件）—— 里面那个密码对 CLI 起的服务**无效**（实测同一个密码：
    桌面版200、CLI 401）。而 CLI 把它自己的密码只打在自己的 stdout 上，
    那个 stdout 随进程退出就消失了。

    也就是说**那个进程的密码在物理上已经拿不到了**。此时「请手工处理」是
    一句空话 —— 用户能做的只有杀掉它或放弃这条路。所以本站自己接管。
    """
    health = await check_health(base, password)
    if health.ok:
        return health
    # 有东西在听但不健康 —— 分三种，处理方式完全不同：
    if "密码不对" not in health.reason:
        # 「可能刚启动、还没就绪」—— 等一下就好。
        deadline = time.monotonic() + health_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(HEALTH_INTERVAL)
            health = await check_health(base, password)
            if health.ok:
                return health
    if not auto_start:
        raise OpencodeUnavailable(
            f"{base} 上有进程在听，但不可用：{health.reason}",
            detail="自动启动已关闭（--no-opencode-autostart），"
                   "所以没去接管那个进程；去掉该开关即可自动接管",
        )
    # **端口被一个「我们没有密码」的服务占着** —— 实测 2026-10-06：
    # ``service.json`` 是**桌面版独占**的（只有它写那个文件），
    # 里面那个密码对CLI 起的服务无效（实测同一个密码：桌面版 200、
    # CLI 401）。而 CLI 起的那个服务把密码只打在自己 stdout 上，
    # 那个 stdout 早就随进程退出消失了 —— 所以**没有任何办法能拿到它**。
    #
    # 唯一的出路是**接管**：杀掉那个进程、起一个我们自己的（密码从
    # stdout 拿）。否则用户永远卡在这里 —— 「杀进程」这种要求
    # 不该由他来提。
    logger.warning(
        "%s 上有个服务在听但密码不对（多半是之前手工起的 opencode，"
        "它的密码只存在于它自己的 stdout，已经拿不到了）—— "
        "正在接管：杀掉它并起一个新的", base,
    )
    killed = _take_over_port(host, port)
    if not killed:
        raise OpencodeUnavailable(
            f"{base} 上有个服务在听但密码不对，而且杀不掉它",
            detail=(
                "那个进程不属于当前用户，或它正被别的程序使用。"
                f"用 `lsof -nP -iTCP:{port}` 找到它，手动 kill 之后重试"
                "（或用 --no-opencode-check 跳过检查）"
            ),
        )
    if is_listening(host, port, timeout=2.0):
        raise OpencodeUnavailable(
            f"杀掉 {base} 上那个进程后端口仍被占用",
            detail="可能有两个进程在抢。用 "
                   f"`lsof -nP -iTCP:{port}` 全找出来清掉，"
                   "或换一个端口（改 .env 里的 OPENPROXY_OPENCODE_BASE）",
        )
    return None



async def ensure_opencode(
    base: str,
    password: str | None,
    *,
    auto_start: bool = True,
    health_timeout: float = HEALTH_TIMEOUT,
    fake_cli: list[str] | None = None,
    service_json: Path | None = None,
) -> Health | Started:
    """确保 ``base`` 上有一个**可用**的 opencode，否则抛 :class:`OpencodeUnavailable`。

    返回 :class:`Health`（本来就有进程）或 :class:`Started`（本站起的）。

    抛异常的三种情况：CLI 找不到 / 起不来 / 起了但不健康。**每一种都带一句
    「接下来该做什么」** —— 只说「失败了」等于把排查成本原样退回给用户。
    """
    endpoint = local_endpoint(base)
    if endpoint is None:
        # 非本机地址：用户自己接的外部服务，本站不接管它的生命周期。
        return await check_health(base, password)

    host, port = endpoint
    actual_password = password or read_password()

    if is_listening(host, port):
        existing = await _handle_existing_listener(
            base, host, port, actual_password, health_timeout, auto_start,
        )
        if existing is not None:
            return existing

    if not auto_start:
        raise OpencodeUnavailable(
            f"{base} 上没有 opencode 在听，而自动启动已关闭"
            f"（--no-opencode-autostart）",
            detail=f"手动起：opencode serve --port {port} --hostname {host}",
        )

    cli = find_cli()
    if cli is None:
        raise OpencodeUnavailable(
            "PATH 里找不到 opencode CLI，无法自动启动",
            detail="装一个（`brew install opencode`），或手动起一个再让"
                   "openproxy 连它，或加 --no-opencode-autostart 跳过检查",
        )

    logger.info("没检测到 %s 上的 opencode，正在用 %s 启动一个", base, cli)
    proc, spawned_password = await _spawn(cli, host, port, fake_cli=fake_cli)
    # **刚起的这个进程的密码优先于配置里的那个** —— 实测 2026-10-06：
    # `.env` 里存着上一个 opencode 的密码（`serve` 每次重启都换新的），
    # 于是新起的进程被自己的旧密码挡住，401 死循环到超时。
    # 而用户留空的场景（本该是常态）下``password`` 是 None，不受影响。
    final_password = spawned_password or password or read_password(service_json)

    deadline = time.monotonic() + health_timeout
    health = Health(False, "还没就绪")
    while time.monotonic() < deadline:
        await asyncio.sleep(HEALTH_INTERVAL)
        health = await check_health(base, final_password)
        if health.ok:
            break
        if proc.poll() is not None:
            raise OpencodeUnavailable(
                f"opencode 启动过程中退出了（退出码 {proc.returncode}）",
                detail=f"手动跑 `opencode serve --port {port} --hostname {host}` "
                       "看它报什么",
            )
    if not health.ok:
        # 留一个半死不活的进程比没有更糟：它会占着端口让下次也起不来。
        proc.kill()
        raise OpencodeUnavailable(
            f"opencode 起了但 {health_timeout:.0f}s 内仍不可用：{health.reason}",
            detail="手动跑一次看它卡在哪；若日志里有认证错误，"
                   "检查 ~/.config/opencode/service.json 里的密码",
        )

    # **不报模型数** —— 真的 opencode 返回的是空列表（见 check_health 的
    # docstring），报出来会让人以为「一个模型都没有」。
    logger.info("opencode 已在 %s 就绪", base)
    return Started(
        process=proc, base=base, password=final_password,
        model_count=health.models,
    )


def resolve_password(explicit: str | None) -> str | None:
    """显式配置优先，否则自动读。

    「显式优先」而不是反过来：用户可能故意把密码设成别的（比如多个
    opencode 实例用不同密码）。而留空时自动读 —— 因为那个密码每次重启都变，
    手填必然过期（实测踩过）。
    """
    return explicit or read_password()
