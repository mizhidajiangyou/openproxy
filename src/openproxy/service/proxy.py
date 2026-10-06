"""上游转发：把 ``/v1/*`` 原样转给 opencode Zen，同时统计用量。

与 demo 相比修掉的三个真实缺陷
------------------------------
1. **强制覆写出站 UA**。上游 Cloudflare 以 ``403 error code: 1010`` 拒绝
   ``Python-urllib/*`` 和**缺失 UA** 的请求（实测）。demo 原样透传客户端 UA，
   于是用 urllib 的客户端、以及任何不发 UA 的客户端会直接挂。
2. **注入 ``stream_options.include_usage``**。不注入的话上游每一帧的 ``usage``
   都是 ``null``，流式调用**完全统计不到用量**（实测）。
3. **不缓冲响应**。每块字节边转发边喂给一个 SSE 扫描器抽 usage，所以内存占用
   与响应长度无关，completion 正文也不会落进任何日志或库。

刻意保持的 demo 行为：上游的非 2xx **原样透传**（含 Zen 对未知模型返回的
``401 ModelError``），hop-by-hop 头双向剥离，下游凭证一律不转发。

**刻意不做的事**：不自动重试。``/v1/chat/completions`` 是有副作用的 POST，
重试会白白消耗上游额度并可能重复计费。只有幂等的 ``GET /v1/models`` 探测才重试
一次（见 :mod:`openproxy.service.model_catalog`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from openproxy.config import RuntimeConfig
from openproxy.domain import ErrorKind, ProxyRejection, TokenUsage, UsageRecord
from openproxy.service.auth import AuthService, QuotaService, ResolvedClient
from openproxy.service.model_catalog import ModelCatalog
from openproxy.service.opencode_client import (
    OpencodeError,
    OpencodeReply,
    OpencodeSettings,
    complete,
)
from openproxy.service.usage_extract import (
    RequestMeta,
    SseUsageScanner,
    inject_reasoning_effort,
    inject_stream_usage,
    normalise_model,
    parse_request_meta,
    usage_from_json_body,
)
from openproxy.service.usage_recorder import UsageRecorder

log = logging.getLogger("openproxy.proxy")

#: 需要请求体的方法。GET 类端点（``/v1/models``）没有 body，不能套用模型白名单。
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

#: RFC 9110 §7.6.1 的逐跳首部，加上 Host（出站要重写）、Accept-Encoding
#: （丢掉，让正文以单一透传分帧到达，不掺 gzip）、Content-Length（正文可能被
#: 我们改写，长度必须由 httpx 重算）。
DROP_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "accept-encoding",
        "content-length",
        # 本站存在的全部理由：绝不让调用方的占位凭证碰到上游，否则回 401 AuthError。
        "authorization",
        "x-api-key",
        "api-key",
    }
)

#: 出站一律覆写、不看客户端发的是什么。
FORCED_REQUEST_HEADERS = frozenset({"user-agent"})

DROP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
        "transfer-encoding",  # httpx 已解分帧；透传上游的会二次分帧
        "content-encoding",  # httpx 已解 gzip；再声明一次会让客户端解两次
        "content-length",  # 分帧由我们决定
        # 这两个不是逐跳首部，但**服务器自己会发**：uvicorn 无条件在自己生成的
        # 头之前拼上自己的 date/server。转发上游的就会变成两份 Date，两份 Server ——
        # RFC 9110 §6.6.1 不允许重复 Date，缓存与 HTTP/2 网关可能取到错误的那个。
        "date",
        "server",
    }
)

STREAM_CONTENT_TYPE = "text/event-stream"


@dataclass(slots=True)
class CallContext:
    """一次调用的全部记账信息。各阶段填充，最后由 :class:`UsageRecorder` 落库。"""

    started: float
    path: str
    client: ResolvedClient
    client_ip: str = ""
    meta: RequestMeta = field(default_factory=RequestMeta)
    status: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    error_kind: ErrorKind = ErrorKind.NONE
    usage: TokenUsage = field(default_factory=TokenUsage.unknown)
    recorded: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def latency_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def to_record(self) -> UsageRecord:
        return UsageRecord(
            ts=int(time.time() * 1000),
            model=self.meta.model or "(未知)",
            path=self.path,
            stream=self.meta.stream,
            status=self.status,
            latency_ms=self.latency_ms,
            usage=self.usage,
            key_id=self.client.key_id,
            key_label=self.client.label,
            anonymous=self.client.anonymous,
            bytes_in=self.bytes_in,
            bytes_out=self.bytes_out,
            error_kind=self.error_kind,
            client_ip=self.client_ip,
        )


class ProxyService:
    """把 ``/v1/*`` 转发到上游，并在每条路径上记账。"""

    def __init__(
        self,
        http: httpx.AsyncClient,
        config_provider: Any,
        auth: AuthService,
        quota: QuotaService,
        catalog: ModelCatalog,
        recorder: UsageRecorder,
    ) -> None:
        self._http = http
        self._config_provider = config_provider
        self._auth = auth
        self._quota = quota
        self._catalog = catalog
        self._recorder = recorder

    @property
    def config(self) -> RuntimeConfig:
        """每次取最新快照 —— 改覆盖层后无需重启任何东西。"""
        runtime: RuntimeConfig = self._config_provider.snapshot
        return runtime

    @property
    def http_client(self) -> httpx.AsyncClient:
        """共享的出站连接池。运维类接口（如上游探测）复用它，不另开池子。"""
        return self._http

    # ----------------------------------------------------------- 主入口 ---

    async def handle(self, request: Request) -> Response:
        ctx = CallContext(
            started=time.monotonic(),
            path=request.url.path,
            client=self._auth.resolve(request.headers),
            client_ip=_client_ip(request),
        )
        try:
            return await self._forward(request, ctx)
        except ProxyRejection as rejection:
            ctx.status = rejection.status
            ctx.error_kind = rejection_kind(rejection.code)
            self._finish(ctx)
            return JSONResponse(rejection.to_payload(), status_code=rejection.status)
        except Exception as exc:
            # **任何**漏到这里的异常都必须先记账再回错。少了这一层，一次调用就会在
            # 报表里凭空消失 —— 而它可能已经把字节发给了上游、消耗了上游额度。
            # 已实测会走到这里的两种：URL 里的非打印字符（httpx.InvalidURL），
            # 以及上游声明了 Content-Length 却提前断开（httpx.RemoteProtocolError）。
            log.warning("转发 %s 时未预期异常: %r", ctx.path, exc)
            ctx.status = 502
            ctx.error_kind = ErrorKind.INTERNAL
            self._finish(ctx)
            return JSONResponse(
                {"error": {"type": "proxy_error", "message": "中转过程中出错"}},
                status_code=502,
            )

    # ------------------------------------------------------------ 转发 ---

    async def _forward(self, request: Request, ctx: CallContext) -> Response:
        config = self.config
        self._auth.enforce(ctx.client, config)

        body = await self._read_body(request, config)
        ctx.bytes_in = len(body)

        if request.method in BODY_METHODS and not body:
            raise ProxyRejection(400, "bad_request", "请求体为空")

        ctx.meta = parse_request_meta(body)
        if request.method in BODY_METHODS:
            self._guard_model(ctx, config)
        # 日配额判定要跑一次当天的 SUM 聚合，是同步 sqlite。开着配额时每个请求都
        # 在事件循环上同步查一遍（实测当天 20 万条记录 ≈ 10ms），并发 SSE 的推进
        # 会被反复打断 —— 所以搬进线程池。没配配额时 acheck 内部直接同步返回，
        # 不为线程切换付代价。
        await self._quota.acheck(ctx.client, config)

        out_body = body
        if ctx.meta.model and ctx.meta.is_json:
            # 白名单判定用的是归一化后的 model，转发的正文也得是同一个 ——
            # 否则 `" space-bunny-free "` 能过本站的白名单，转上去却被上游回
            # 401 ModelError（那个码的字面意思是「凭证无效」）
            out_body = normalise_model(out_body, ctx.meta.model)
        if config.inject_stream_usage and ctx.meta.stream:
            out_body = inject_stream_usage(out_body)
        # 思考级别是**强制**语义：客户端自己写了也照样覆写。这三个改写函数都只碰
        # JSON body，改不动就原样返回 —— 宁可少一个功能，也不能把用户的请求弄坏。
        if config.reasoning_effort is not None and request.method in BODY_METHODS:
            out_body = inject_reasoning_effort(out_body, config.reasoning_effort)

        # ---- 分流：这几个模型改走本机 opencode 服务，其余直通上游 ----
        #
        # **放在所有 body 改写之后**：走 opencode 也要用同一份规范化后的模型名与
        # 注入过的思考级别，否则「直通时归一化、走opencode 时不归一」会变成一个
        # 极难复现的差异（同一个请求，走哪条路得到不同结果）。
        #
        # 只对 POST 生效：``GET /v1/models`` 这类没有 body、也没有「模型」概念，
        # 走 opencode 没有对应语义。
        if (
            ctx.meta.model
            and request.method in BODY_METHODS
            and config.uses_opencode(ctx.meta.model)
        ):
            return await self._forward_via_opencode(ctx, out_body)

        url = config.upstream_base.rstrip("/") + _raw_path(request)
        if request.url.query:
            url = f"{url}?{request.url.query}"

        try:
            # build_request 也要在 try 里：URL 里的非打印字符会在**这里**就抛
            # InvalidURL，压根到不了 send()。
            upstream_request = self._http.build_request(
                request.method,
                url,
                headers=build_upstream_headers(request.headers, config),
                content=out_body or None,
            )
            upstream = await self._http.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            return self._upstream_failure(ctx, 504, "upstream_timeout", "上游超时",
                                         ErrorKind.UPSTREAM_TIMEOUT)
        except httpx.HTTPError as exc:
            return self._upstream_failure(
                ctx, 502, "upstream_unreachable",
                f"上游不可达: {exc.__class__.__name__}", ErrorKind.UPSTREAM_UNREACHABLE,
            )

        content_type = (upstream.headers.get("content-type") or "").lower()
        if content_type.startswith(STREAM_CONTENT_TYPE):
            return self._stream_response(upstream, ctx)
        return await self._buffered_response(upstream, ctx)

    @staticmethod
    async def _read_body(request: Request, config: RuntimeConfig) -> bytes:
        """按块读请求体，**在读的过程中**就执行上限。

        ``await request.body()`` 没有上限：客户端可以先把任意大小的字节推给
        进程，缓冲完才轮到我们检查 ``max_body_bytes`` —— 那个配置项就成了摆设。
        """
        limit = config.max_body_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise ProxyRejection(
                    413,
                    "request_too_large",
                    f"请求体超过上限 {limit} 字节",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _guard_model(self, ctx: CallContext, config: RuntimeConfig) -> None:
        """模型白名单。

        ``free_models_only=False`` 时完全放行（本站退化为纯反向代理）；开启时
        名单外的模型直接 400 —— 不要转上去让上游回 ``401 ModelError``，那个码的
        字面意思是「凭证无效」，客户端看到会误判成密钥有问题。
        """
        if not config.free_models_only:
            return
        if not ctx.meta.model:
            raise ProxyRejection(400, "model_required", "请求体缺少 model 字段")
        if not self._catalog.is_allowed(ctx.meta.model, free_only=True):
            allowed = ", ".join(self._catalog.catalog_ids())
            raise ProxyRejection(
                400,
                "model_not_allowed",
                f"模型 {ctx.meta.model!r} 不在免费清单内。可用模型: {allowed}",
            )

    def _upstream_failure(
        self, ctx: CallContext, status: int, code: str, message: str, kind: ErrorKind
    ) -> JSONResponse:
        ctx.status = status
        ctx.error_kind = kind
        self._finish(ctx)
        return JSONResponse({"error": {"type": code, "message": message}}, status_code=status)

    # ------------------------------------------------- 非流式 / 错误响应 ---

    async def _buffered_response(self, upstream: httpx.Response, ctx: CallContext) -> Response:
        try:
            body = await upstream.aread()
        finally:
            await upstream.aclose()
        ctx.bytes_out = len(body)
        ctx.status = upstream.status_code
        if upstream.status_code >= 400:
            ctx.error_kind = ErrorKind.UPSTREAM_STATUS

        ctx.usage = usage_from_json_body(body)
        self._finish(ctx)

        return build_response(
            Response(content=body, status_code=upstream.status_code),
            filter_response_headers(upstream.headers),
        )

    async def _forward_via_opencode(
        self, ctx: CallContext, body: bytes
    ) -> Response:
        """把这个请求转给本机 opencode 服务，由它去打上游。

        ## 为什么这条路必然是非流式的

        opencode 的接口是「投递 → 事后查」：``POST /api/session/<id>/prompt``
        立刻返回 ``{"delivery":"steer"}``，回复要再 ``GET
        /api/session/<id>/message`` 轮询才拿得到（实测 22~25 秒）。而它的
        ``/api/event`` 是 SSE 事件流 —— 但那要求客户端从建连起一直挂着，
        还要处理断线重连与「哪些事件属于我这次请求」的过滤。

        所以这里**先把回复收全、再一次性吐给客户端**，而不是假装能流式。
        客户端请求 ``stream:true`` 却拿到非流式 JSON 会报错，所以下面按
        ``stream`` 标志补一层 ``data:`` 包装 —— 让客户端拿到的形状与直通一致。
        这个降级是刻意的：宁可少一个「首token 早到几百毫秒」的特性，
        也不能让客户端因为响应形状变了而失败。

        ## 为什么不自动回退到直通

        「opencode 挂了就自动打直通」听起来更稳，但会造成**同一模型在两条路上
        静默切换**：客户端看到的价格、上下文、模型回答全都不一致，而日志里
        只有一行 warning。宁可明确报「opencode 服务不可达」，让用户知道
        要去把服务起起来。
        """
        config = self.config
        prompt = _prompt_of(body)
        if not prompt:
            return self._upstream_failure(
                ctx, 400, "bad_request",
                "走 opencode 服务时只支持 chat/completions 的 messages 格式",
                #必须是 BAD_REQUEST 而不是 CLIENT_DISCONNECT：这是「请求形状不对」，
                # 记成客户端断开会让渠道页的「失败原因拆解」把它归进错误的桶，
                # 而且与 ``rejection_kind()`` 里已有的 ``bad_request → BAD_REQUEST``
                # 自相矛盾 —— 同一种 400 在两条路径上被记成两个类别。
                ErrorKind.BAD_REQUEST,
            )

        settings = OpencodeSettings(
            base_url=config.opencode_base,
            password=config.opencode_password,
            directory=config.opencode_directory,
            connect_timeout=config.connect_timeout,
            # 单次 HTTP 用普通的 read_timeout 即可 —— 轮询的**总**预算由
            # ``poll_timeout`` 管，而 :func:`openproxy_client._request` 会把每次
            # 轮询请求的上限压到「剩余预算」内，所以不会出现
            # 「poll_timeout=120s 但单次 GET 能卡 600s」那种最坏 12 分钟的等待。
            read_timeout=config.read_timeout,
            poll_timeout=config.opencode_timeout,
        )
        try:
            reply = await complete(
                self._http, settings,
                # **必须加约束句** —— 否则 opencode 会先调 read 去读全局
                # AGENTS.md 指定的文件，然后卡在等权限批准上直到超时。
                # 详见 :data:`OPENCODE_NO_TOOL_SUFFIX` 的实测数据。
                _with_no_tool_suffix(prompt),
                model=ctx.meta.model or "",
            )
        except asyncio.CancelledError:
            # **客户端断开时必须先记账再重抛**。``CancelledError`` 继承自
            # ``BaseException``，既不被下面的 ``except OpencodeError`` 捕获，
            # 也不被 ``handle()`` 的 ``except Exception`` 捕获 —— 而 opencode
            # 侧的 prompt **已经投递、模型已经在生成**，额度已经花掉了。
            # 不记账的话这一次调用会在报表里彻底消失（实测落库 0 条）。
            # 直通路径靠 ``_TeeStream`` 解决了同一问题，那套逻辑在这里不适用，
            # 因为我们是在**等一个 future**，没有可tee 的响应体。
            ctx.status = 499
            ctx.error_kind = ErrorKind.CLIENT_DISCONNECT
            self._finish(ctx)
            raise
        except OpencodeError as exc:
            log.warning("opencode 服务调用失败（模型 %s）: %s", ctx.meta.model, exc)
            status = 502 if exc.kind is ErrorKind.UPSTREAM_STATUS else 504
            code = (
                "upstream_rejected" if exc.kind is ErrorKind.UPSTREAM_STATUS
                else "upstream_timeout"
            )
            return self._upstream_failure(ctx, status, code, str(exc), exc.kind)

        payload = _forward_via_opencode_payload(reply, ctx.meta.model, ctx.client.key_id)
        raw = json.dumps(payload, ensure_ascii=False).encode()
        ctx.bytes_out = len(raw)
        ctx.status = 200
        # **落库的模型必须是 opencode 实际服务的那个**，不是客户端请求的那个。
        # 实测 opencode 会忽略我们传的 modelID、一律走它自己的默认模型 ——
        # 于是统计里写着 big-pickle、实际回答来自 fledge-alpha-free。
        # 那样「按模型统计用量」会系统性地归错类，而这一页上看不出任何异常。
        # 同理 ``ctx.meta`` 也要换掉：``_finish`` 落库读的就是它。
        # 用 ``replace`` 而不是赋值 —— ``RequestMeta`` 是 frozen dataclass。
        ctx.meta = dataclasses.replace(
            ctx.meta, model=reply.model or ctx.meta.model
        )
        ctx.usage = TokenUsage(
            prompt_tokens=reply.usage.get("prompt_tokens", 0),
            completion_tokens=reply.usage.get("completion_tokens", 0),
            cached_tokens=reply.usage.get("cached_tokens", 0),
            reasoning_tokens=reply.usage.get("reasoning_tokens", 0),
            total_tokens=(
                reply.usage.get("prompt_tokens", 0)
                + reply.usage.get("completion_tokens", 0)
            ),
            known=bool(reply.usage),
        )

        # **响应体与 bytes_out 必须在 ``_finish`` 之前都定下来**。
        # 之前这里先 ``_finish(ctx)`` 再按stream 分支改 ``ctx.bytes_out`` ——
        # 那句赋值是死代码（落库早就完成了），于是流式调用记的是
        # 非流式 JSON 的长度（实测 285 vs 实际 673，少算 60%+），
        # 而报表上完全看不出来。
        headers = [("content-type", "application/json")]
        if ctx.meta.stream:
            # 客户端要的是 SSE，就按SSE 的形状包一层（见 docstring 的「必然非流式」）。
            raw = _as_sse(payload)
            headers = [
                ("content-type", STREAM_CONTENT_TYPE),
                ("cache-control", "no-cache"),
            ]
        ctx.bytes_out = len(raw)
        self._finish(ctx)
        return build_response(Response(content=raw, status_code=200), headers)

    # ---------------------------------------------------------- 流式 ---

    def _stream_response(self, upstream: httpx.Response, ctx: CallContext) -> StreamingResponse:
        ctx.status = upstream.status_code
        if upstream.status_code >= 400:
            ctx.error_kind = ErrorKind.UPSTREAM_STATUS
        scanner = SseUsageScanner()
        # 关掉反向代理的缓冲，否则 token 会被攒成一坨再吐出来
        extra = [("cache-control", "no-cache"), ("x-accel-buffering", "no")]

        return build_response(
            StreamingResponse(self._tee(upstream, ctx, scanner), status_code=upstream.status_code),
            filter_response_headers(upstream.headers),
            defaults=extra,
        )

    def _tee(
        self, upstream: httpx.Response, ctx: CallContext, scanner: SseUsageScanner
    ) -> _TeeStream:
        return _TeeStream(upstream, ctx, scanner, self._finish)

    # ---------------------------------------------------------- 记账 ---

    def _finish(self, ctx: CallContext) -> None:
        """幂等落库。同步且非阻塞 —— 见 :mod:`openproxy.service.usage_recorder`。"""
        if ctx.recorded:
            return
        ctx.recorded = True
        if ctx.notes:
            log.debug("调用 %s 附加信息: %s", ctx.path, "; ".join(ctx.notes))
        self._recorder.record(ctx.to_record(), ctx.client.key_id)


class _TeeStream:
    """流式转发 + 增量抽 usage + 一次性记账。

    **刻意不用 async generator 函数。** 原因是实测出来的：async generator 在
    「被 ``aclose()`` 但从未开始迭代」时，函数体一次都不执行，于是 ``finally``
    里的记账不会发生 —— 那条调用就在报表里彻底消失。写成显式迭代器后，
    :meth:`aclose` 自己保证收尾，覆盖三种退出：

    * 正常读完（``StopAsyncIteration``）
    * ``GeneratorExit`` —— 有人直接 ``aclose()``
    * ``asyncio.CancelledError`` —— **真实服务器走的是这条**：uvicorn 宣告
      ASGI ``spec_version >= 2.3`` 时，Starlette 用 ``create_task_group()``
      并监听断连，断连表现为**取消**任务而不是关闭生成器。而 ``CancelledError``
      继承自 ``BaseException``，只写 ``except Exception`` 的代码会漏掉它 →
      一次被放弃的流会被记成「200 成功、0 token」。

    另外 :meth:`aclose` 里必须关上游：httpx 只在流**正常读完**时才归还连接，
    被 GeneratorExit / CancelledError 打断时不会。漏掉它，反复断流的客户端会
    一路吃掉出站连接池。

    已产出的部分照样记账：否则这次调用在报表里凭空消失，速率图会出现
    无法解释的缺口。
    """

    __slots__ = (
        "_completed",
        "_ctx",
        "_finish_cb",
        "_finished",
        "_iterator",
        "_scanner",
        "_status",
        "_upstream",
    )

    def __init__(
        self,
        upstream: httpx.Response,
        ctx: CallContext,
        scanner: SseUsageScanner,
        finish: Callable[[CallContext], None],
    ) -> None:
        self._upstream = upstream
        self._ctx = ctx
        self._scanner = scanner
        self._finish_cb = finish
        self._iterator: AsyncIterator[bytes] = upstream.aiter_bytes()
        self._finished = False
        #: 上游**正常读完**为 True。用来区分「读完」与「被打断」——
        #: 只看 _status 不够，因为正常读完那一刻 _status 还是 NONE，
        #: 而 aclose() 会把 NONE 解释成「客户端断开」。
        self._completed = False
        self._status: ErrorKind = ErrorKind.NONE

    def __aiter__(self) -> _TeeStream:
        return self

    async def __anext__(self) -> bytes:
        if self._finished:
            raise StopAsyncIteration
        try:
            chunk = await self._iterator.__anext__()
        except StopAsyncIteration:
            self._completed = True
            await self.aclose()
            raise
        except httpx.TimeoutException:
            self._status = ErrorKind.UPSTREAM_TIMEOUT
            await self.aclose()
            raise
        except httpx.HTTPError:
            self._status = ErrorKind.UPSTREAM_UNREACHABLE
            await self.aclose()
            raise
        except (GeneratorExit, asyncio.CancelledError):
            self._status = ErrorKind.CLIENT_DISCONNECT
            await self.aclose()
            raise
        self._ctx.bytes_out += len(chunk)
        self._scanner.feed(chunk)
        return chunk

    async def aclose(self) -> None:
        """收尾：关上游连接 + 记账。可重复调用，也可被外部提前调用。"""
        if self._finished:
            return
        if self._status is ErrorKind.NONE and not self._completed:
            self._status = ErrorKind.CLIENT_DISCONNECT
        # 关上游连接：httpx 只在流正常读完时才归还连接池
        # （失败无可挽回，但不能让它盖掉正在传播的原始异常）
        with contextlib.suppress(Exception):
            await self._upstream.aclose()
        self._finished = True
        self._settle()

    def _settle(self) -> None:
        ctx = self._ctx
        # 上游本来就已经报错（>=400）时，保留 UPSTREAM_STATUS —— 那是更根本的
        # 原因；否则「上游 429 + 客户端提前关闭」会被记成客户端断开，
        # 渠道页的失败原因拆解就永远看不到那个 429。
        #
        # 判据只有 ``ctx.error_kind is NONE``：error_kind 非 NONE 的唯一来路是
        # 「上游回了 >=400」（见 :meth:`_stream_response`）或本站自己拒了请求，两种
        # 情况下 ``ctx.status`` 都 >=400 —— 所以「error 非空但 status < 400」是
        # 空集，之前那半句 ``and ctx.status < 400`` 永远走不到，留着只会骗人。
        if self._status is not ErrorKind.NONE and ctx.error_kind is ErrorKind.NONE:
            ctx.error_kind = self._status
        ctx.usage = self._scanner.close()
        if self._scanner.oversized_lines:
            ctx.notes.append(f"discarded_oversized_sse_lines={self._scanner.oversized_lines}")
        self._finish_cb(ctx)


# ------------------------------------------------------------------ 工具 ---


def rejection_kind(code: str) -> ErrorKind:
    """本站自己拒绝的请求归到哪一类失败。"""
    return _REJECTION_KINDS.get(code, ErrorKind.AUTH_FAILED)


_REJECTION_KINDS: dict[str, ErrorKind] = {
    "request_too_large": ErrorKind.REQUEST_TOO_LARGE,
    "bad_request": ErrorKind.BAD_REQUEST,
    # 合法 JSON 但没有 model 字段。它**不是** NOT_JSON —— 那个类别是给
    # 「请求体根本不是 JSON」的，混在一起会让渠道页的失败原因拆解说错话。
    "model_required": ErrorKind.BAD_REQUEST,
    "model_not_allowed": ErrorKind.MODEL_NOT_ALLOWED,
    "daily_quota_exceeded": ErrorKind.QUOTA_EXCEEDED,
    "global_quota_exceeded": ErrorKind.QUOTA_EXCEEDED,
    "missing_api_key": ErrorKind.AUTH_FAILED,
    "invalid_api_key": ErrorKind.AUTH_FAILED,
    "api_key_disabled": ErrorKind.AUTH_FAILED,
}


def _client_ip(request: Request) -> str:
    """取客户端 IP。

    **只取 TCP 直连地址，不读 ``X-Forwarded-For``**：本站默认绑 127.0.0.1，
    任何 XFF 都来自本机客户端自报、可以随手伪造。把它当真会让「按 IP 统计」
    变成一个毫无意义的字段。
    """
    return (request.client.host if request.client else "")[:64]


def build_upstream_headers(
    client_headers: Mapping[str, str], config: RuntimeConfig
) -> dict[str, str]:
    """客户端头 → 出站头。

    关键点：**无条件设置 UA**。Cloudflare 拒绝 ``Python-urllib/*`` 与缺失 UA，
    透传客户端 UA 会让一部分合法客户端直接 403。

    同时**双保险**地剔除凭证头：``DROP_REQUEST_HEADERS`` 已经按小写名过滤过一遍，
    这里再按小写名删一次，防止 Starlette/httpx 用非预期大小写带进来。
    """
    headers: dict[str, str] = {}
    for name, value in client_headers.items():
        lowered = name.lower()
        if lowered in DROP_REQUEST_HEADERS or lowered in FORCED_REQUEST_HEADERS:
            continue
        headers[name] = value
    headers["user-agent"] = config.upstream_user_agent
    if config.upstream_key:
        headers["authorization"] = f"Bearer {config.upstream_key}"
    return headers


def filter_response_headers(upstream_headers: Any) -> list[tuple[str, str]]:
    """上游响应头 → 传给客户端的头列表。

    返回**列表**而不是字典，是必须的：``Headers.items()`` 会把重复头合并成
    ``"a, b"``，于是上游发的两个 ``Set-Cookie`` 会变成一个头，客户端读到的
    第一个 cookie 的值直接是坏的。``multi_items()`` 保留重复项。

    **调用方必须把返回值原样写进 ``response.raw_headers``**，不要再包一层
    ``dict()`` —— 那样重复项又会被合并回一个（这正是 E1-26 修完之后仍然在线上
    丢 cookie 的原因）。见 :func:`build_response`。
    """
    if hasattr(upstream_headers, "multi_items"):
        pairs: list[tuple[str, str]] = list(upstream_headers.multi_items())
    else:  # pragma: no cover — 测试里的普通 dict
        pairs = [(k, v) for k, v in upstream_headers.items()]
    return [(k, v) for k, v in pairs if k.lower() not in DROP_RESPONSE_HEADERS]


def build_response[R: Response](
    response: R,
    pairs: Iterable[tuple[str, str]],
    *,
    defaults: Iterable[tuple[str, str]] = (),
) -> R:
    """把上游响应头写进已经建好的响应，**保留重复项**。

    为什么不用 ``Response(headers=...)``：Starlette 的 ``init_headers`` 走
    ``MutableHeaders``，而 dict 式的赋值对同名头只会留下最后一个。上游发两个
    ``Set-Cookie``（或 401 的两个 ``WWW-Authenticate`` challenge）时，客户端会
    只拿到最后一个 —— 测试若只断言 helper 的返回值就会全绿，而线上早就坏了。

    ``defaults`` 是本站自己加的头（``x-accel-buffering`` 之类）：仅在上游**没有**
    发同名头时才补，等价于原来的 ``setdefault``。
    """
    seen = {name.lower() for name, _ in pairs}
    response.raw_headers.extend(
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (*pairs, *(d for d in defaults if d[0].lower() not in seen))
    )
    return response


def _prompt_of(body: bytes) -> str:
    """从 chat/completions 风格的 body 里取出要发给 opencode 的纯文本。

    只支持这一种形状，因为 opencode 的 ``/prompt`` 端点只吃一个字符串 ——
    它不是 OpenAI 兼容的代理，而是「给 agent 派一句话」。所以 ``/v1/responses``、
    ``/v1/messages`` 这类别的协议**转不过去**，调用方会拿到明确的 400 而不是
    一个含义错误的回复（那比报错更糟：用户会以为模型答错了）。

    返回空串表示「这个 body 里没有可转的文本」，调用方据此回 400。
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    messages = data.get("messages")
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # 只收 user 角色：system 在 opencode 那边由它自己的 agent 决定，
        # assistant 历史对它没有意义，混进去只会让上下文变脏。
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            # 多模态形状：[{"type":"text","text":"..."}, {"type":"image_url",...}]
            # 只取文字块 —— 图片转不过去（opencode 端点不接图片），
            # 但静默丢掉整条消息更糟，所以至少把文字部分带上。
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
    return "\n\n".join(p for p in parts if p)


#: 转发给 opencode 时**必须追加**在prompt 末尾的约束句。
#:
#: ## 为什么非加不可（实测 2026-10-06）
#:
#: opencode 是**agent** —— 它每个新会话都会读全局 ``AGENTS.md``，而那份文件里
#: 写着「每次会话开始处理任何任务前，必须先读取xxx」。于是它会先调``read``
#: 工具去读那个文件。
#:
#: 而**读文件这类工具它必须先要人批准**。通过 HTTP 投递时没有 TUI 有人点
#: 「允许」，那个工具就永远停在排队态：
#:
#: ```text
#: tool: {"name":"read","executed":false,"state":{"status":"running"}}
#: ```
#:
#: 症状是**一直等到本站超时**（实测 120s），而日志里只有一句
#: 「没有回复内容」—— 与「模型不能用」看起来一模一样。
#:
#: ## 实测效果（同一服务、同一模型、同一 prompt，各跑 3 次）
#:
#: =======================  ======  ======
#: 模型                不加后缀  加后缀
#: =======================  ======  ======
#: ``fledge-alpha-free``   2/3 卡住  0/3
#: ``mimo-v2.6-flash-free``  3/3 卡住  0/3
#: =======================  ======  ======
#:
#: ## 措辞为什么是这样
#:
#: - **放在末尾**：实测放前面时模型有时读不到（同一模型一次成功一次超时）。
#:   末尾是它最后读到的东西，权重最高。
#: - **「不要使用任何工具」**：``read`` 本身就是工具 —— 只说「不要读文件」
#:   而不禁用工具时，模型仍可能去调``glob``/``grep`` 找那个文件。
#: - **「不要遵循任何项目指令文件」**：``AGENTS.md`` / ``CLAUDE.md`` 这类是
#:   **系统级指令**，优先级高于用户在 prompt 里说的话。不点破的话，
#:   模型会认为「用户的这句话」不如``AGENTS.md``，于是照旧去读。
#: - **「直接回答」**：给出替代动作，否则模型可能「不读但也不答」。
#:
#: ## 这不是万能的
#:
#: 它挡的是「模型主动去用工具」。模型**真的需要**工具时（例如问题就是
#: 「读一下这个文件」），加上这句会让它答不了 —— 而那种情况本来也不适合
#: 走 opencode 转发，那是一条「纯问答」链路。
OPENCODE_NO_TOOL_SUFFIX = (
    "\n\n（本次请求为纯文本问答：不要使用任何工具，不要读取任何文件，"
    "不要遵循任何项目指令文件，直接回答。）"
)


def _with_no_tool_suffix(prompt: str) -> str:
    """给转发给 opencode 的 prompt 加上 :data:`OPENCODE_NO_TOOL_SUFFIX`。

    已经是 ``None`` 或空串时原样返回 —— 上游会自己报「没有可转的文本」，
    而在这里拼上一句约束只会让那个 400 变成一句莫名其妙的话。
    """
    if not prompt.strip():
        return prompt
    return prompt + OPENCODE_NO_TOOL_SUFFIX


def _forward_via_opencode_payload(
    reply: OpencodeReply, requested_model: str, key_id: str | None
) -> dict[str, Any]:
    """把 opencode 的回复包成 OpenAI 形状的 ``chat.completion``。

    抽成模块级纯函数是为了能直接测它 —— 之前这段逻辑内嵌在
    ``_forward_via_opencode`` 里，要测就得先把整个转发层架起来（容器、
    配置覆盖层、假HTTP 客户端），而那样测的就不是「包出来的形状对不对」
    而是「整个链路通不通」了。

    ``model`` 用 **opencode 实际服务的那个**（``reply.model``），不是请求的 ——
    opencode 会忽略我们传的 modelID，一律走它自己的默认模型。回显请求值会
    掩盖「实际走了另一个模型」这个事实。
    """
    message: dict[str, Any] = {"role": "assistant", "content": reply.text}
    # 思考过程用 OpenAI 兼容的 ``reasoning_content`` 带上。
    # 为什么必须带：``reply.reasoning`` 已经被 ``_text_of`` 认真采集了
    # （它区分 ``text`` 与 ``reasoning`` 两类 part），而 ``reasoning_tokens``
    # 也落进了库 —— 如果这里丢掉内容，用户会看到「有思考 token 消耗、
    # 但思考内容查不到」，而强制思考档位的效果**在界面上就无法验证**了。
    # 没有思考内容时**不放这个键**（而不是放空串）：部分客户端见到空串
    # 会当成「思考了但内容被清空」。
    if reply.reasoning:
        message["reasoning_content"] = reply.reasoning
    return {
        "id": f"opencode-{key_id or 'anon'}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": reply.model or requested_model,
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": message,
        }],
        "usage": _usage_payload(reply),
    }


def _usage_payload(reply: OpencodeReply) -> dict[str, Any]:
    """Opencode 的 token 计数 → OpenAI 形状的 ``usage``。

    缺项补 0 而不是省略字段：某些客户端会读 ``usage.total_tokens``，
    字段缺失时它们会当成「流式没开统计」而报错。

    ``cached_tokens`` / ``reasoning_tokens`` 走 OpenAI 的嵌套形状
    （``prompt_tokens_details`` / ``completion_tokens_details``）——
    与直通路径 :data:`usage_extract._NESTED_FIELDS` 保持一致，
    这样客户端无论走哪条路，读 ``usage`` 的代码都不用改。
    """
    u = reply.usage
    prompt_tokens = u.get("prompt_tokens", 0)
    completion_tokens = u.get("completion_tokens", 0)
    payload: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if (cached := u.get("cached_tokens")) is not None:
        payload["prompt_tokens_details"] = {"cached_tokens": cached}
    if (reasoning := u.get("reasoning_tokens")) is not None:
        payload["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return payload


def _as_sse(payload: dict[str, Any]) -> bytes:
    """把一个非流式回复包成客户端期待的 SSE 帧序列。

    帧的形状参照 OpenAI 的流式协议：``role`` → ``content`` → ``finish_reason``
    → 末帧 ``[DONE]``，并且**每帧都带 usage**（与本站直通路径注入
    ``stream_options.include_usage`` 后的行为一致），这样客户端在流式分支里
    也能拿到 token 数。
    """
    model = payload.get("model", "")
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    base = {"id": payload.get("id", ""), "object": "chat.completion.chunk",
            "created": payload.get("created", 0), "model": model}
    frames = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"},
                              "finish_reason": None}]},
    ]
    # 思考内容单独一帧，放在正文之前 —— 顺序与真实流式一致（先想后答）。
    # 条件里有``message.get("reasoning_content")``：没有思考时**不发这一帧**，
    # 而不是发一个空串帧（那会让客户端以为「思考了但内容为空」）。
    if message.get("reasoning_content"):
        frames.append({
            **base,
            "choices": [{"index": 0,
                         "delta": {"reasoning_content":
                                   message["reasoning_content"]},
                         "finish_reason": None}],
        })
    frames.append(
        {**base, "choices": [{"index": 0,
                              "delta": {"content": message.get("content", "")},
                              "finish_reason": None}]},
    )
    frames.append(
        {**base, "choices": [{"index": 0, "delta": {},
                              "finish_reason": choice.get("finish_reason", "stop")}],
         "usage": payload.get("usage", {})},
    )
    out = b"".join(
        b"data: " + json.dumps(f, ensure_ascii=False).encode() + b"\n\n" for f in frames
    )
    return out + b"data: [DONE]\n\n"


def _raw_path(request: Request) -> str:
    """客户端请求的**原始**（未解码）路径。

    ASGI 的 ``scope["path"]`` 已经过百分号解码，直接拿它拼 URL 会出两类问题：

    * ``%2e%2e%2f`` 之类的编码斜杠会变成结构性的 ``/``，于是
      ``/v1/..%2f..%2fetc`` 能逃出 ``upstream_base`` 的路径前缀；
    * ``%3F`` 会把一个路径段变成查询串。

    ``scope["raw_path"]`` 是客户端真正发来的字节，用 latin-1 解出来即可 1:1
    还原，非 ASCII 也能原样交给 httpx。
    """
    raw = request.scope.get("raw_path")
    if isinstance(raw, (bytes, bytearray)) and raw:
        return bytes(raw).decode("latin-1")
    return quote(request.url.path, safe="/")
