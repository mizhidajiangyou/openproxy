"""应用装配：路由挂载、静态站点、lifespan、错误处理。

层与层的边界在这一层定死：
``api`` 依赖 ``service``，``service`` 依赖 ``store`` + ``domain``，
``store`` 依赖 ``domain``，``domain`` 谁都不依赖。往回依赖会在 import 期形成环。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from openproxy.api import routes_admin, routes_relay
from openproxy.config import Settings, load_settings
from openproxy.container import Container
from openproxy.domain import ProxyRejection

#: 前端站点目录。``app.py`` 在 ``<repo>/src/openproxy/``，所以仓库根是 parents[2]。
#: 多写一个 ``parents`` 会指到仓库的**上一级目录**，静态站会静默 404 而不是报错。
WEB_ROOT = Path(__file__).resolve().parents[2] / "web"

log = logging.getLogger("openproxy")


def configure_logging(level: str = "INFO") -> None:
    """一条极简格式。**不打请求/响应正文** —— 正文永远不进日志。"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    start_pruner: bool = True,
    start_prober: bool | None = None,
    tz_offset_minutes: int | None = None,
) -> FastAPI:
    """构造 ASGI 应用。

    ``settings`` / ``transport`` / ``start_prober`` / ``tz_offset_minutes`` 主要给测试与
    自定义部署用；生产走 :func:`openproxy.__main__.main` → :func:`load_settings`。
    """
    resolved = settings if settings is not None else load_settings()
    container = Container.build(
        resolved, transport=transport, tz_offset_minutes=tz_offset_minutes
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await container.startup(start_pruner=start_pruner, start_prober=start_prober)
        log.info(
            "openproxy 已启动 | 转发 %s -> %s | 免鉴权=%s",
            f"http://{resolved.host}:{resolved.port}/v1",
            container.config.snapshot.upstream_base,
            not container.config.snapshot.require_key,
        )
        try:
            yield
        finally:
            await container.shutdown()

    app = FastAPI(
        title="openproxy 中转站",
        description="转发到 opencode Zen 免费模型的网关，带用量统计",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.container = container

    app.include_router(routes_relay.router)
    app.include_router(routes_admin.router)

    @app.get("/api/health", tags=["console"], summary="本站自检")
    async def health() -> dict[str, object]:
        snapshot = container.config.snapshot
        return {
            "ok": True,
            "version": app.version,
            "upstream": snapshot.upstream_base,
            "require_key": snapshot.require_key,
            "recorder_dropped": container.recorder.dropped,
        }

    _install_error_handlers(app)
    _mount_web(app)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProxyRejection)
    async def _on_rejection(_request: Request, exc: ProxyRejection) -> JSONResponse:
        return JSONResponse(exc.to_payload(), status_code=exc.status)

    @app.exception_handler(Exception)
    async def _on_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("未预期异常: %r", exc)
        # 不把异常文本回给客户端：它可能包含上游 URL、文件路径这类内部信息。
        return JSONResponse(
            {"error": {"type": "internal_error", "message": "站内错误，请查看服务端日志"}},
            status_code=500,
        )


def _mount_web(app: FastAPI) -> None:
    """挂载静态站点。

    路由用 hash（``#/overview``），所以后端**不需要** SPA fallback：刷新任何一个
    地址都只会命中 ``/``。这样也避免了「为了一个静态页引入 catch-all 路由」
    把真正的 404 吞掉。
    """
    index = WEB_ROOT / "index.html"
    if not index.exists():  # pragma: no cover — 打包漏了前端资源
        log.warning("未找到前端站点目录 %s，仅提供 API", WEB_ROOT)
        return

    app.mount("/css", StaticFiles(directory=WEB_ROOT / "css"), name="css")
    app.mount("/js", StaticFiles(directory=WEB_ROOT / "js"), name="js")
    app.mount("/assets", StaticFiles(directory=WEB_ROOT / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index_page() -> FileResponse:
        return FileResponse(index, media_type="text/html; charset=utf-8")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(WEB_ROOT / "assets" / "favicon.svg", media_type="image/svg+xml")
