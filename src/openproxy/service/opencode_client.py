"""opencode 本地服务客户端 —— 「直通失败时的第二条路」。

## 为什么存在

实测（2026-10-04）：10 个免费模型里有 9 个从站外直连会被上游回
``403 FreeTierError``（``OpenCode's free tier can only be used from within
OpenCode``），只有 ``space-bunny-free`` 能直连。而**同一个模型经由 opencode
本地服务能调通** —— 因为那个服务本身就是 opencode 客户端，上游认它。

所以本站提供两条可选的上游：

* **直通（默认）**：本站直接打 ``opencode.ai/zen``，保留SSE 流式透传。
  配了 ``OPENPROXY_UPSTREAM_KEY`` 时自动带上 Bearer。
* **opencode 服务**：把请求转给本机 opencode 的 HTTP 服务，由它去打上游。

## 为什么默认仍是直通

opencode 服务有四个实测到的硬限制，每一个都会让「用它当唯一上游」变难：

1. **模型指定不了** —— ``/api/session/<id>/prompt`` 的三种写法
   （``model`` 对象 / ``providerID``+``modelID`` / 配置文件）全部被忽略，
   一律走默认模型（实测两次都拿到 ``fledge-alpha-free``）。
2. **流式变非流式** —— 投递后返回 ``{"delivery":"steer"}``，回复要事后
   ``GET /api/session/<id>/message`` 轮询拿。所以本模块只提供「一次性取回复」。
3. **强依赖外部进程** —— opencode 没起就全 502，版本升级接口可能变。
4. **要维护 session** —— 每个并发请求一个 session，用完得回收。

因此本模块的定位是**按模型粒度的兜底**：只在控制台给某个模型选了
「走 opencode」时才走它，其余模型仍然直通。

## 认证

opencode 的服务用 **HTTP Basic**，用户名固定是 ``opencode``，密码在
``~/.config/opencode/service.json`` 的 ``password`` 字段（``opencode serve``
启动时也会把密码打到 stdout）。这是从二进制里读到的确切实现::

    authorization: "Basic " + Buffer.from(`opencode:${password}`).toString("base64")
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx

from openproxy.domain.models import ErrorKind

#: ``/api/session/<id>/prompt`` 投递后，模型要多久才回完。
#:
#: 为什么要轮询而不是长连接：实测 opencode 没有可用的「等回复」端点
#: （``/message`` 是 GET 查历史），只有 ``/api/event`` 事件流 —— 而那要求
#: 一直保持连接、还要处理断线重连，比轮询复杂得多，而收益只是省几次 HTTP。
#: 25 秒是实测「3+3」这类最小问题的耗时上界（实测 22~25 秒含首 token），
#: 留了余量；复杂问题靠 :attr:`OpencodeSettings.timeout` 放宽。
DEFAULT_POLL_INTERVAL: Final = 0.35
DEFAULT_POLL_TIMEOUT: Final = 120.0

#: ``GET /message`` 里表示「本轮已经结束」的消息类型。
#:
#: ``idle`` = 正常结束；``error`` / ``aborted`` = 出错或被中止。
#: 三者都必须让轮询**立刻退出** —— 它们是终态，继续等只会白等到
#: ``poll_timeout``（实测 120 秒 ≈ 340 次轮询 HTTP，全占着共享连接池），
#: 而 ``error`` 的真实原因会被「超时」两个字盖掉。
#:
#: 用它在 :func:`_await_reply` 里分流：``idle`` 走「取回复或报空回复」，
#: 另外两个走「把错误细节带进异常」。
_TERMINAL_TYPES: Final = frozenset({"idle", "error", "aborted"})


@dataclass(frozen=True, slots=True)
class OpencodeSettings:
    """连opencode 服务要用的参数。全部有可用的默认值。"""

    base_url: str = "http://127.0.0.1:4096"
    """opencode 服务地址。用 ``opencode serve --port N`` 时填对应端口。"""

    password: str | None = field(default=None, repr=False)
    """Basic 认证密码。``None`` 时从 :func:`discover_password` 自动找。

    ``repr=False``：明文口令绝不能进``repr()`` —— 它会出现在
    「打印配置看看」这类调试代码、异常回溯、以及测试失败输出里。
    ``_auth``（算出来的 Basic 头）同样是 ``repr=False``。
    """

    directory: str | None = None
    """工作区目录。opencode 会按它加载配置与项目上下文；``None`` 用服务默认的。"""

    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    poll_interval: float = DEFAULT_POLL_INTERVAL
    poll_timeout: float = DEFAULT_POLL_TIMEOUT

    _auth: str | None = field(default=None, repr=False, compare=False)
    """缓存的认证头。``None`` = 还没算过。

    **必须是显式字段而不是 ``@cached_property``**：本类是 ``slots=True``，
    没有 ``__dict__``，而 ``cached_property`` 恰恰靠 ``__dict__`` 存缓存，
    访问时会抛 ``TypeError: No '__dict__' attribute``。
    （实测踩过：先按「frozen 仍有 ``__dict__``」的印象写了 cached_property，
    一跑就炸。``slots=True`` 与 ``frozen=True`` 是两件独立的事。）

    ``__post_init__`` 会把它清空，所以 ``dataclasses.replace()`` 复制一个
    算过认证头的实例时**不会**把旧的头带过去 —— 否则改密码后
    ``replace(inst, password="new")`` 仍会用 old 的头（实测确认）。
    ``compare=False`` 让 ``==`` 也不受它影响。

    **注意 ``asdict()`` 仍会包含它**（明文 Basic 头）。所以**不要**把
    这个对象整个丢给 ``asdict`` /日志序列化 —— 要传就只传需要的字段。
    当前代码没有这样的用法（``repr=False`` 保证 ``repr()`` 是干净的）。
    """

    def __post_init__(self) -> None:
        # 每次构造（含 replace 产生的副本）都清空缓存。
        # 不这么做的代价很隐蔽：改密码后不生效，而 docstring 承诺
        # 「密码变更会在下次请求时生效」。
        object.__setattr__(self, "_auth", None)

    def auth_header(self) -> str | None:
        """``Basic base64("opencode:<password>")``，读不到密码则 ``None``。

        **必须缓存**：这个方法在 ``_headers()`` 里被调用，而 ``_headers()``
        每个 HTTP 请求调一次 —— 一次 ``complete()`` 至少 3次，轮询期间每
        ``poll_interval`` 还要再加一次（实测 120 秒超时 ≈ 340 次）。
        不缓存就是**同步阻塞文件 IO 跑在事件循环上**，而且在共享连接池的
        每个请求的关键路径上。
        """
        if self._auth is None:
            pw = self.password or discover_password()
            if pw:
                raw = f"opencode:{pw}".encode()
                # object.__setattr__：frozen dataclass 的常规绕过写法。
                object.__setattr__(
                    self, "_auth", "Basic " + base64.b64encode(raw).decode()
                )
        return self._auth

@dataclass(frozen=True, slots=True)
class OpencodeReply:
    """一次 ``prompt`` 的最终结果。"""

    text: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    reasoning: str = ""


class OpencodeError(RuntimeError):
    """调opencode 服务失败。``kind`` 决定上游回给客户端的状态码。"""

    def __init__(self, message: str, *, kind: ErrorKind = ErrorKind.UPSTREAM_UNREACHABLE) -> None:
        super().__init__(message)
        self.kind = kind


def discover_password(config_path: Path | None = None) -> str | None:
    """从 ``~/.config/opencode/service.json`` 读服务密码。

    刻意**读文件而不是让用户手填**：那个文件是 opencode 自己写的
    （``opencode serve`` 启动时把同一个密码打到 stdout并存进去），
    读它就不会因为用户改了密码而失效 —— 而手填的密码一旦opencode
    重新生成就悄悄403 了。

    ``config_path`` 只给测试用：默认 ``None`` =读标准位置。做成参数而不是
    让测试去 monkeypatch 模块里的 ``Path`` —— 那种改法会让mypy 抱怨
    「模块没有显式导出 Path」，而且它依赖的是``Path.home`` 这个实现细节。

    **用 ``utf-8-sig`` 读** —— 与 ``.env`` 同理：Windows 记事本 / VS Code
    「以 UTF-8 with BOM 保存」是默认行为，而 ``utf-8`` 不剥 BOM 会让
    ``json.loads`` 直接抛「Expecting value」—— 而那被``except`` 吞掉之后
    的症状是「密码读不到」而不是「文件坏了」，极难联想。
    """
    path = config_path or (Path.home() / ".config" / "opencode" / "service.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    pw = data.get("password")
    return pw if isinstance(pw, str) and pw else None


def _headers(settings: OpencodeSettings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if auth := settings.auth_header():
        headers["Authorization"] = auth
    if settings.directory:
        headers["x-opencode-directory"] = settings.directory
    return headers


def _is_connect_failure(detail: str) -> bool:
    """判断 502 的 body 是不是「连不上」。

    实测桌面版（2026-10-05）：opencode 连不上它自己的上游时，会回
    ``502`` 且 body 是 ``upstream connect failed: Connection refused (os error 61)``。
    而**它自己没在跑**时，httpx 会在更外层抛 ``ConnectError`` —— 两者都归成
    「上游不可达」，但处置完全不同，所以要分开提示。
    """
    lowered = detail.lower()
    return ("connect failed" in lowered
            or "connection refused" in lowered
            or "econnrefused" in lowered)


async def _request(
    client: Any,
    settings: OpencodeSettings,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout_override: float | None = None,
) -> Any:
    """一次 HTTP 调用，统一把 opencode 的错误结构翻译成 :class:`OpencodeError`。

    opencode 用 Effect 的 ``_tag`` 风格返回错误（``{"_tag":"UnauthorizedError",...}``），
    所以这里按 ``_tag`` 判类型而不是 HTTP 状态码 —— 404 意味着「路径不对/会话没了」，
    400 意味着「参数不对」，两者都不该报成「服务不可达」。

    ``timeout_override``：把这一次请求的上限压到调用方给的预算内。
    **轮询时必须用** —— 否则 ``poll_timeout`` 只在 ``while`` 头部检查，
    管不住「正在飞的那一次请求」：实测 poll_timeout=0.3s 而单次 GET 卡 2s时，
    实际耗时 2.00s（6.7 倍预算）；生产默认值下最坏总时长会变成
    ``poll_timeout(120s) + 单次 GET 卡满(600s) = 12 分钟``。
    """
    url = settings.base_url.rstrip("/") + path
    read_timeout = (
        settings.read_timeout if timeout_override is None
        else min(settings.read_timeout, max(timeout_override, 1.0))
    )
    try:
        resp = await client.request(
            method, url, json=payload, headers=_headers(settings),
            # **必须显式给 connect**，不能只给标量 timeout：标量会覆盖
            # connect/read/write/pool 四类，于是「连接本地服务要 2 秒、
            # 等回复要 600 秒」这种需求表达不出来。实测传标量 600 时
            # connect 也变成了 600 秒 —— 本机端口被占时要多等 10 分钟才报错。
            timeout=httpx.Timeout(read_timeout, connect=settings.connect_timeout),
        )
    except Exception as exc:  # httpx 的异常层级较杂，这里统一归类
        # **连接层失败要区分「没人监听」与「连上了但超时」** ——
        # 这两个的处置完全不同，而合并成一句「服务不可达」会让人只能猜。
        #
        # 实测 2026-10-05（用户踩了这个坑）：``opencode_base`` 默认是
        # ``http://127.0.0.1:4096``，而 opencode 实际跑在**桌面版的随机端口**
        # （当次是 49374）-> Connection refused。控制台只显示
        # 「上游不可达」，用户完全看不出是「地址配错了」还是「服务没起」。
        #
        # 而桌面版端口**每次启动都变**，所以这个提示必须带上实际连的地址。
        hint = ""
        if isinstance(exc, httpx.ConnectError):
            # 这条是**httpx 层**的失败，所以能确定「本机连不上 opencode_base」
            # —— 服务没起、或地址/端口不对、或被代理挡了（此时报错会是
            # 「连接代理失败」，与「连接 opencode 失败」文本不同）。
            #
            # 但**不能**断言「opencode 没在跑」—— 也可能是 opencode_base
            # 写错了（比如桌面版端口每次启动都变）。
            hint = (
                f"（本机连不上 {settings.base_url} —— "
                f"要么 opencode 没在跑，要么 opencode_base 配错了"
                f"（桌面版端口每次启动都变，建议用 "
                f"`opencode serve --port 4096` 固定端口），"
                f"要么请求被 http_proxy 环境变量绕进了代理）"
            )
        elif isinstance(exc, httpx.TimeoutException):
            hint = (
                f"（连上了 {settings.base_url} 但超时 {read_timeout:.0f}s —— "
                f"服务在跑但没响应，可能是它正忙或卡在某个请求上）"
            )
        raise OpencodeError(
            f"opencode 服务不可达：{exc}{hint}",
            # 连不上就是 unreachable，不用额外说明 —— 默认值正是它。
        ) from exc

    if resp.status_code >= 400:
        detail = resp.text[:300]
        kind = ErrorKind.UPSTREAM_UNREACHABLE
        if resp.status_code in (401, 403):
            # 认证不通是**配置问题**，不是上游不可达 —— 别混为一谈，
            # 否则控制台上会显示「上游不可达」而真正原因是密码不对。
            kind = ErrorKind.UPSTREAM_STATUS
            hint = (
                "（密码不对。去 ~/.config/opencode/service.json 核对，"
                "或设 OPENPROXY_OPENCODE_PASSWORD）"
            )
        elif _is_connect_failure(detail):
            # opencode 自己会把它连不上的上游包成 502 再回给我们
            # （实测桌面版就这样）。而**本机这条链路**上的 502 也可能来自
            # 完全另一个东西 —— 比如某个中间代理。
            #
            # 实测 2026-10-05（用户踩的坑）：``http_proxy``环境变量让 httpx
            # 把本该直连 ``127.0.0.1:4096`` 的请求转发给了
            # ``127.0.0.1:50572``，那个代理连不上上游就回
            # ``502 upstream connect failed`` —— 与 opencode 自己的错误
            # **文本一模一样**，光看 body 分不出是谁在说。
            #
            # 所以提示语不能说「opencode 可达」—— 那是我第一版的写法，
            # 实测正好是错的（4096 上根本没有 opencode，只有那个代理）。
            # 改成只给可确定的事实 + 让用户去核对。
            hint = (
                f"（收到 502 且内容是「连不上」—— 但**不能确定是谁连不上**："
                f"可能是 opencode 自己要转发到的上游，也可能是本机链路上的"
                f"某个代理。逐个核对：① opencode 是否在跑、"
                f"opencode_base 是否正确（当前 {settings.base_url}）；"
                f"② 环境变量 http_proxy/https_proxy 是否把本机直连绕进了代理"
                f"（httpx 默认会读它们，本站已设 trust_env=False））"
            )
        else:
            hint = ""
        raise OpencodeError(
            f"opencode 返回 {resp.status_code}：{detail}{hint}", kind=kind,
        )

    return resp.json()


async def list_models(client: Any, settings: OpencodeSettings) -> list[str]:
    """opencode 当前可用的模型 id 清单。

    给控制台「哪些模型可以走 opencode」用。服务没起时返回空列表而不是抛错 ——
    因为这是个**探测性**调用，不该因为它把整页炸掉。
    """
    try:
        data = await _request(client, settings, "GET", "/api/model")
    except OpencodeError:
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [
        str(m.get("modelID") or m.get("id"))
        for m in items
        if isinstance(m, dict) and (m.get("modelID") or m.get("id"))
    ]


async def complete(
    client: Any,
    settings: OpencodeSettings,
    prompt: str,
    *,
    model: str,
) -> OpencodeReply:
    """跑一轮 prompt 并等它回完。

    流程（每一步都是实测出来的，路径与字段名不是猜的）：

    1. ``POST /api/session`` 建会话，**模型在这一步指定**，返回
       ``{"data":{"id":"ses_..."}}``
    2. ``POST /api/session/<id>/prompt`` 投递，返回 ``{"delivery":"steer"}``
    3. 轮询 ``GET /api/session/<id>/message``，等出现 ``type=="assistant"``
    4. ``DELETE /api/session/<id>`` 回收会话

    第 4 步用 ``finally`` 保证：即使中途抛错也不会漏下会话 —— 否则长时间运行
    会在 opencode 里堆出一堆空会话。

    ## 模型必须**建会话时**指定（我曾搞错过，且错得很贵）

    形状是::

        {"model": {"id": "<modelID>",
                   "providerID": "opencode",
                   "modelID": "<modelID>"}}

    ``id`` 与 ``modelID`` **都要给**：只给 ``{providerID, modelID}`` 会被拒成
    ``400 Missing key at ["model"]["id"]``（实测 2026-10-05）。

    **建好会话之后 ``/prompt`` 不接受任何 model 参数** —— 顶层
    ``{providerID, modelID}``、嵌套 ``{"model": {...}}``、``{"model": "字符串"}``
    三种形状实测**全部被静默忽略**：都返回 200、都``delivery:"steer"``，
    但实际服务的都是 opencode 的默认模型。

    **「返回 200」不等于「参数被接受」。** 我曾据此得出「opencode 改不了模型」
    的结论，还把它写进 README、代码注释与 ``task.md``，并据此建议你
    「放弃这条路、去注册账号」。那是**错的** —— 而错的代价是让一个可行的
    方案被当成死路。判定必须去看 ``GET /message`` 里 assistant 消息的
    ``model.id``，而不是看 HTTP 状态码。

    实测（2026-10-05，真实 opencode 2.0.20 与桌面版 2.0.22 都成立）：
    建会话时指定 ``mimo-v2.6-flash-free`` -> 实际服务的就是它。
    """
    session_body: dict[str, Any] = {}
    if model:
        # 三字段都带上：``id`` 是 opencode 用来认模型的主键（缺了 400），
        # ``modelID`` 与 ``providerID`` 一起构成它的 Model.Ref。
        session_body["model"] = {
            "id": model, "providerID": "opencode", "modelID": model,
        }
    try:
        created = await _request(
            client, settings, "POST", "/api/session", session_body
        )
    except OpencodeError:
        raise
    sid = (created.get("data") or {}).get("id") if isinstance(created, dict) else None
    if not sid:
        raise OpencodeError(
            "opencode 未返回 session id（建会话的响应里没有 data.id）",
            # 不是「连不上」而是「上游返回了预期外的东西」—— 实测 2026-10-06
            # 有个模型调不通时正是这样，而它被归到 unreachable 后，
            # 控制台上显示的是「上游不可达」，把方向带偏成查地址/端口/代理。
            kind=ErrorKind.UPSTREAM_STATUS,
        )

    try:
        await _request(
            client, settings, "POST", f"/api/session/{sid}/prompt",
            # **不带任何 model 参数** —— 实测在这里传会被静默忽略
            # （见 docstring）。模型已经在建会话时定好了。
            {"text": prompt},
        )
        return await _await_reply(client, settings, sid, model)
    finally:
        # 回收会话。失败无所谓（可能已被清理），但不能让它盖掉真正的异常。
        with contextlib.suppress(Exception):
            await _request(client, settings, "DELETE", f"/api/session/{sid}")


async def _await_reply(
    client: Any, settings: OpencodeSettings, sid: str, requested: str = ""
) -> OpencodeReply:
    """轮询直到出现 assistant 消息或本轮结束。

    ## 为什么必须按消息去重（而不是只累加新增）

    ``GET /api/session/<id>/message`` 返回的是**到目前为止的全部历史**，
    不是增量 —— opencode 侧的查询（``messages.sql`` 的
    ``ListMessagesBySession``）是 ``SELECT * FROM messages WHERE session_id = ?
    ORDER BY created_at ASC``，**没有 limit、没有 since 参数**。

    而轮询会重复调它十几次到几十次（默认 ``poll_interval=0.35s``，
    实测最小问题 22~25 秒 ≈ 60~70 次）。所以「把每条 assistant 的内容拼起来」
    这个正确做法，在不去重的前提下会把早期消息**累加几十次** ——
    用户拿到一个重复几十次的答案，HTTP 200、没有任何报错。

    实测（2026-10-05，忠实复刻「历史只增不减」语义）：3 次轮询 ->
    「我先读一下配置。」出现 3 次；按生产参数会到几十次。

    ``seen`` 让每条消息只被累加一次。**优先用消息 id**，没有 id 时退回
    内容指纹 —— 见 :func:`_mark_seen`。

    ## 关键：「见到过」不等于「采到过」

    opencode 的 assistant 消息是**先以空 parts 入库、再原地增长**的：

    - ``agent.go:326``：``messages.Create(..., Parts: []message.ContentPart{})``
    - ``agent.go:455-485``：每个流式 delta 都 ``messages.Update(...)``，
      而 ``UpdateMessage`` 是 ``UPDATE messages SET parts = ? WHERE id = ?``
      —— **同一 id 原地覆盖**
    - ``agent.go:507``：``TrackUsage`` 在 ``EventComplete`` 时才写 token

    所以第一次轮询很可能看到 ``content=[] tokens={}``。若此时就把它标记成
    「已处理」，之后内容增长也不再采-> **答案 100% 丢失并误报「没有回复内容」**
    （实测：4 次轮询、答案造好第 3 次到达，仍然抛异常）。

    按默认 ``poll_interval=0.35s``，而实测最小问题总耗时 22~25 秒、首token
    之前的推理时间通常远超 350ms —— 所以这个空窗**是常态而非边缘情况**。

    因此用**两个集合**：

    - ``seen``：见到过（用来跳过重复的**空**消息，省掉无谓的解析）
    - ``collected``：采到过（决定是否累加）

    空内容的消息只进 ``seen`` 不进 ``collected``，所以下一轮内容涨上来时
    仍会被采集。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.poll_timeout
    path = f"/api/session/{sid}/message"
    best = OpencodeReply(text="", model="")
    seen: set[str] = set()
    collected: set[str] = set()

    while loop.time() < deadline:
        #把这一次的单次 HTTP 上限压到**剩余预算**内。否则 poll_timeout
        # 只在 while 头部管事，管不住「正在飞的那一次 GET」——
        # 而这正是「opencode 进程挂起但 TCP 不断」时的等待来源。
        remaining = deadline - loop.time()
        data = await _request(client, settings, "GET", path,
                              timeout_override=remaining)
        messages = _iter_messages(data)
        # **先扫完整轮，再判断是否结束** —— 消息顺序不保证。
        #
        # 实测（2026-10-05，真实 opencode 服务跑「7*8 是几」）：返回的顺序是
        #   [user, **idle**, assistant[reasoning+text], user]
        # ——``idle`` 排在 assistant **之前**。而逐条遍历时一遇到 idle 就
        # 检查 best -> 此时 assistant 还没被处理 -> 误判「没有回复内容」。
        # 那个 case 在真实环境下**每次都触发**（实测 180 秒超时）。
        #
        # 所以：**error/aborted 仍然立刻抛**（那是真正的失败），
        # 而 idle 只记「本轮已结束」，等这一轮所有消息都处理完再决定返回。
        finished = False
        for msg in messages:
            kind = str(msg.get("type") or "")
            if kind in _TERMINAL_TYPES and kind != "idle":
                raise OpencodeError(
                    f"opencode 本轮{ '被中止' if kind == 'aborted' else '出错' }"
                    f"：{_error_detail_of(msg)}",
                    kind=ErrorKind.UPSTREAM_STATUS,
                )
            if kind == "assistant":
                # **assistant 消息自带两个错误字段**，不读就会把上游故障
                # 误报成「没有回复内容」—— 实测 2026-10-05：
                #   finish="error" + error={...:"Endpoint is unavailable"}
                #   retry.attempt=2 + retry.error={...:"provider.rate-limit"}
                # 两者都是**这条消息自己的失败状态**，而 ``type`` 仍是
                # ``assistant``（所以按 type 判断完全看不到）。
                detail = _assistant_failure(msg)
                if detail:
                    raise OpencodeError(
                        f"opencode 调用 {model_hint_of(msg)} 失败：{detail}",
                        kind=ErrorKind.UPSTREAM_STATUS,
                    )
            if kind == "idle":
                finished = True
                continue
            if kind != "assistant":
                continue
            key = _message_key(msg)
            if key in collected:
                # 已经采过这一轮的内容 —— 历史每次都全量返回，跳过。
                continue
            if key not in seen:
                seen.add(key)
            # 空窗消息会被反复解析（这是有意的）：opencode 的消息是
            # **先以空 parts 入库、再原地增长**的（见 docstring），
            # 所以「见过但当时是空的」必须重新看一次。
            text, reasoning = _text_of(msg)
            usage = _usage_of(msg)
            if not (text or reasoning):
                # 空窗：只记「见过」，**不**记「采到」——
                # 这样下一轮内容长上来时仍会被采集。
                continue
            collected.add(key)
            model = str((msg.get("model") or {}).get("id") or "")
            best = OpencodeReply(
                # 正文与思考都要**拼接**，不是覆盖。
                # 一轮 prompt 产生多条 assistant 消息是**常态**：opencode 是
                # agent，工具调用一轮会产生一条（文本 + tool_call），拿到
                # tool_result 后再产生一条。实测（2026-10-05，真实 opencode
                # 服务跑「读 /etc/hosts」）：
                #   第 1 条 content=[reasoning, tool] tokens={}
                # 即「我先读一下配置」这类中间文本会是一条独立消息。
                # 写成 ``text or best.text`` 的话它会被后一条整个顶掉 ——
                # 用户拿到一个**语义不完整但看起来正常**的答案，没有任何报错。
                # （前提是已经按 key 去重，否则拼接会重复累加—— 见 docstring。）
                text=best.text + text if best.text else text,
                reasoning=best.reasoning + reasoning if best.reasoning else reasoning,
                model=model or best.model,
                usage=_merge_usage(best.usage, usage),
            )

        # 这一轮处理完了再看要不要结束 —— 顺序不保证，idle 可能在 assistant 前。
        if finished:
            # ``or best.reasoning``：模型可能整轮只在思考而没出正文
            # （实测存在这种轮次）。那时报「没有回复内容」是误导 ——
            # 真因是「只思考没答」，而思考内容我们**已经采到了**，
            # 丢掉它等于白花上游额度。
            if best.text or best.reasoning:
                return best
            raise OpencodeError(
                "opencode 本轮结束但没有回复内容"
                "（若 opencode 侧要求权限确认，它不会自己回答）",
                # **不是 unreachable**。实测 2026-10-06：多个模型调不通时
                # opencode 会在 1.5 秒内回一个 ``idle`` 且不带任何 assistant
                # 消息 —— 那是「那个模型的上游端点挂了」，不是「opencode
                # 连不上」。归错类会让渠道页显示「上游不可达」，
                # 而处置该是「换个模型」。
                kind=ErrorKind.UPSTREAM_STATUS,
            )
        await asyncio.sleep(settings.poll_interval)

    # 超时时**还没有回复**，所以 ``best.model`` 是空的 —— 必须报调用方请求的模型名，
    # 否则错误信息是「模型 (未知)」，而用户明明指定了 big-pickle。
    # 两个都带上：请求的（用户以为在用哪个）与实际走的（opencode 会用它自己的
    # 默认模型，两者可能不同），这样一眼能看出是不是走偏了。
    raise OpencodeError(
        f"opencode 超过 {settings.poll_timeout:.0f}s 仍未回复"
        f"（请求模型 {requested or '(未指定)'}，实际走的 {model_hint(best)}）",
        # **必须显式给``kind=UPSTREAM_TIMEOUT``**（实测 2026-10-06）：
        # 不给会落成默认的 ``unreachable``，于是控制台上「渠道」页把这几条
        # 归到「上游不可达」—— 而它们明明是「等太久了」。两者处置完全不同：
        # 不可达要查地址/端口/代理，超时要查opencode 侧为什么慢。
        # 判据是耗时：120410ms 正好等于 opencode_timeout（120s）。
        kind=ErrorKind.UPSTREAM_TIMEOUT,
    )


def _iter_messages(data: Any) -> list[dict[str, Any]]:
    """从 ``GET /message`` 的返回里取出消息数组。

    opencode 可能返回 ``{"data":[...]}`` 或直接是数组 —— 两种都认。
    """
    if isinstance(data, dict):
        items = data.get("data")
    elif isinstance(data, list):
        items = data
    else:
        return []
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def _text_of(msg: dict[str, Any]) -> tuple[str, str]:
    """从一条 assistant 消息里取（正文，思考过程）。

    ``content`` 是分节的：``type=="text"`` 是正文，``type=="reasoning"`` 是思考。
    两者都要 —— 只取正文的话，强制思考档位的效果在控制台上看不见。
    """
    text_parts: list[str] = []
    reason_parts: list[str] = []
    for part in msg.get("content") or []:
        if not isinstance(part, dict):
            continue
        chunk = part.get("text")
        if not isinstance(chunk, str):
            continue
        if part.get("type") == "text":
            text_parts.append(chunk)
        elif part.get("type") == "reasoning":
            reason_parts.append(chunk)
    return "".join(text_parts), "".join(reason_parts)


def _message_key(msg: dict[str, Any]) -> str:
    """算消息的去重键。**纯函数**（不做集合操作，判定在调用处）。

    优先级：

    1. **消息 id**（``msg_*``）。这是 opencode 数据库里的主键，稳定且唯一。
    2. **内容指纹** —— 只在消息没有 id 时用。

    为什么要有第 2 条兜底：``GET /message`` 的返回形状是 Effect 的 schema，
    理论上可能某个版本不带 ``id``。那种情况下若没有指纹兜底，
    就会退化成「每条都被当成新消息」-> 重复累加 —— 也就是这个键
    要防的那个 bug。所以宁可「内容相同就当重复」（极小的误判代价：
    模型真的连续说了两遍一模一样的话会被合并），也不要「重复累加」。

    用 ``id`` 时加前缀，避免「某条无 id 消息的指纹恰好等于另一条的 id」
    这种跨类型碰撞。

    ``default=str`` 是**纵深防御**：指纹是对 ``content`` 做 ``json.dumps``，
    万一遇到不可序列化的值（现实中不会——它来自 ``json.loads``），
    不该让整个转发层炸成 502。与 :func:`_error_detail_of` 的兜底一致。
    """
    mid = msg.get("id")
    if isinstance(mid, str) and mid:
        return f"id:{mid}"
    parts = msg.get("content")
    if isinstance(parts, list):
        try:
            return "fp:" + json.dumps(parts, ensure_ascii=False, sort_keys=True,
                                      default=str)
        except (TypeError, ValueError):  # pragma: no cover — 上面已 default=str
            return "fp:" + str(parts)
    return "fp:" + str(msg.get("model"))


def _merge_usage(
    prev: dict[str, int], new: dict[str, int]
) -> dict[str, int]:
    """合并多条 assistant 消息的 token 计数。

    ## 为什么不能直接用最后一条的

    实测（2026-10-05，真实 opencode 服务跑「读 /etc/hosts」）：
    工具调用那一轮的 assistant 消息 ``tokens`` 是**空对象** ``{}``，
    而最终答案那条有完整计数。所以如果无脑覆盖，
    「先有统计、后被空对象清空」就会让这次调用的 token 变成「未知」——
    而它在报表里看起来只是「这次没统计到」，没有任何异常提示。

    ## 为什么取 max 而不是相加

    ``input`` 在多轮里是**累计值**（每一轮都包含之前所有轮的内容，
    所以第二轮的 input 天然大于第一轮），相加会把前面几轮重复计一遍。
    ``output``/``reasoning`` 逐轮递增，取 max 同样正确。

    空的一侧视为「这一轮没报」，保留旧值 —— 这正是上面那个实测场景需要的。

    ## 「max 偏小比偏大安全」这句话是错的（第四轮 review 的 B-3-2）

    曾写过「如果哪天opencode 改成报增量，max 会偏小（少算）而不是偏大
    （多算），少算比多算安全」。**这个论证不成立**：opencode 确实存在让
    ``input`` **递减**的路径 —— 摘要器压缩历史后会把它清零
    （``agent.go`` 的 ``oldSession.PromptTokens = 0``）。此时 max 会取到
    **压缩前**那个更大的值，于是多算。

    但**不能改成「取末值」**：末值可能是 0（刚被清零）或者某轮只报了
    ``output`` 没报 ``input``，那样会**少算得更离谱**，而少算会让用户
    以为没花额度。

    所以保留 max，并把这句话改成实话：**max 在「累计口径」下正确，
    在「摘要压缩」这种会主动清零的路径下会多算**。这是已知的保守偏差 ——
    宁可高估也不要低估，因为高估只会让用户多留意一点，低估会让账单看起来
    比实际便宜。真要精确就得自己维护 session 级累加器，那是更大的改动。
    """
    return {
        key: max(prev.get(key, 0), new.get(key, 0))
        for key in (*prev, *new)
    }


def model_hint_of(msg: dict[str, Any]) -> str:
    """从 assistant 消息里取模型名（用于错误信息）。"""
    model = msg.get("model")
    if isinstance(model, dict):
        name = model.get("id")
        if isinstance(name, str) and name:
            return name
    return "(未知)"


def _assistant_failure(msg: dict[str, Any]) -> str:
    """assistant 消息自带的失败信息；没有就返回空串。

    ## 为什么必须看这两个字段

    实测 2026-10-05（真 opencode，``mimo-v2.5-free`` 与``ling-3.0-flash-fin-free``）：
    这两个模型调不通时，消息的 ``type`` 仍然是 ``assistant``、``content`` 是 ``[]``，
    真正的失败写在：

    - ``finish == "error"`` + ``error.message``（例：
      ``provider.invalid-request`` /「Upstream request failed: Endpoint is unavailable」）
    - ``retry.attempt`` + ``retry.error.message``（例：``provider.rate-limit``，
      表示它在自动重试，尚未放弃）

    只按 ``type`` 与 ``content`` 判断的话，这两种情况都会被误报成
    「本轮结束但没有回复内容」—— 而真因是**上游那个模型的端点挂了**，
    那个信息只有这两个字段里有。报错了用户才能知道「换个模型试试」。
    """
    if msg.get("finish") == "error":
        return _error_detail_of({"error": msg.get("error")}) or "上游返回 error"
    retry = msg.get("retry")
    if isinstance(retry, dict) and retry.get("error"):
        attempt = retry.get("attempt")
        prefix = f"（第 {attempt} 次重试仍失败）" if isinstance(attempt, int) else ""
        detail = _error_detail_of({"error": retry.get("error")})
        return f"{prefix}{detail or '上游重试失败'}"
    return ""


def _error_detail_of(msg: dict[str, Any]) -> str:
    """从 ``type in ("error", "aborted")`` 的消息里取出可读的原因。

    opencode 的错误消息形状不固定：``error`` 消息里错误信息可能放在
    ``error`` / ``message`` / ``reason`` 下，也可能整个 ``payload`` 就是一个
    字符串。所以这里挨个试，找不到就把原始 JSON 截一段 —— 宁可给出不好看但
    有信息的内容，也不要只报「出错」两个字让人无从下手。
    """
    for key in ("error", "message", "reason", "detail"):
        value = msg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
        if isinstance(value, dict):
            nested = value.get("message") or value.get("name")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()[:200]
    payload = msg.get("payload")
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:200]
    try:
        return json.dumps(msg, ensure_ascii=False)[:200]
    except (TypeError, ValueError):  # pragma: no cover — msg 来自 json.loads
        return repr(msg)[:200]


def _usage_of(msg: dict[str, Any]) -> dict[str, int]:
    tokens = (msg.get("tokens") or {})
    out: dict[str, int] = {}
    for src, dst in (("input", "prompt_tokens"), ("output", "completion_tokens"),
                     ("reasoning", "reasoning_tokens")):
        v = tokens.get(src)
        if isinstance(v, int) and v > 0:
            out[dst] = v
    cache = tokens.get("cache") or {}
    if isinstance(cache.get("read"), int) and cache["read"] > 0:
        out["cached_tokens"] = cache["read"]
    # **刻意不采集 ``cache.write``（cache_creation）**：本项目的 ``TokenUsage``
    # 没有对应字段，而**直通路径也不拆它**（``usage_extract`` 只读
    # ``prompt_tokens_details.cached_tokens``）。若这里单独加一个字段，
    # 两条路的 ``usage`` 口径就不一样了 —— 而客户端看到的形状必须一致。
    #
    # opencode 自己是用独立 SQL 统计它的（``json_extract(..., '$.tokens.cache.write')``），
    # 所以那个数字存在，只是本站不展示。要展示得先给 ``TokenUsage`` 与
    # ``usage_records`` 加列，那是另一次改动（牵涉 schema 迁移）。
    return out


def model_hint(reply: OpencodeReply) -> str:
    return reply.model or "(未知)"
