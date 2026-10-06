"""``/v1/*`` 透传路由。

只有一个 catch-all 处理器，因为中转站的职责是「原样转过去」：
逐个端点写 handler 意味着每加一个上游端点就要改本站代码，漏掉的端点会变成 404。
路径原样拼到上游，语义完全由上游定义。

**声明顺序有意义**：`/v1/__health` 必须排在 ``/v1/{path:path}`` 前面，
否则 ``{path:path}`` 会先把 ``__health`` 吃掉。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from openproxy.api.deps import ProxyDep

router = APIRouter(tags=["relay"])


@router.get("/v1/__health", summary="本站自检")
async def relay_health(proxy: ProxyDep) -> dict[str, Any]:
    """本站自己的健康探针，**不转发到上游**。

    上游对未知模型会回 ``401 ModelError``，如果把它透给客户端，客户端会误读成
    「本站挂了 / 密钥失效」。健康检查必须与上游分开。
    """
    config = proxy.config
    return {
        "ok": True,
        "upstream": config.upstream_base,
        "upstream_authenticated": bool(config.upstream_key),
        "require_key": config.require_key,
        "user_agent": config.upstream_user_agent,
    }


@router.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
    summary="透传到 opencode Zen 的 OpenAI 兼容接口",
)
async def relay(path: str, request: Request, proxy: ProxyDep) -> Response:
    # 实际路径取自 request.url.path（保留原始大小写与百分号编码），这里的
    # `path` 只是路由模板占位。
    _ = path
    return await proxy.handle(request)


def register(app: Any) -> None:
    app.include_router(router)
