"""FastAPI 依赖：从 :class:`~openproxy.container.Container` 取服务对象。

两个容易踩的坑，这里都踩过了并写进注释：

1. **依赖里的参数必须有类型标注。** 写成 ``Depends(lambda c: c.keys)`` 时，
   FastAPI 看不到标注，会把 ``c`` 当成**查询参数**，于是每个请求都在找一个
   ``?c=`` —— 而且失败得很隐蔽（返回 422 "Field required"，看起来像客户端参数错）。
   所以下面一律用具名函数 + 标注。
2. **依赖全部是同步的。** 它们只做属性读取、不做 IO，写成 ``async def`` 只会让
   FastAPI 在每个请求上多跑一次事件循环调度，没有任何收益。
"""

from __future__ import annotations

import hmac
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, Request, status

from openproxy.container import Container
from openproxy.service.auth import AuthService, QuotaService
from openproxy.service.config_service import ConfigService
from openproxy.service.dashboard import DashboardService
from openproxy.service.model_catalog import ModelCatalog
from openproxy.service.proxy import ProxyService
from openproxy.service.usage_recorder import UsageRecorder
from openproxy.store import KeyStore, UsageStore


def get_container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover — 只有 lifespan 没跑时才会出现
        raise HTTPException(status_code=503, detail="服务尚未初始化")
    return cast(Container, container)


ContainerDep = Annotated[Container, Depends(get_container)]


def _keys(container: ContainerDep) -> KeyStore:
    return container.keys


def _usage(container: ContainerDep) -> UsageStore:
    return container.usage


def _config(container: ContainerDep) -> ConfigService:
    return container.config


def _auth(container: ContainerDep) -> AuthService:
    return container.auth


def _quota(container: ContainerDep) -> QuotaService:
    return container.quota


def _catalog(container: ContainerDep) -> ModelCatalog:
    return container.catalog


def _recorder(container: ContainerDep) -> UsageRecorder:
    return container.recorder


def _proxy(container: ContainerDep) -> ProxyService:
    return container.proxy


def _dashboard(container: ContainerDep) -> DashboardService:
    return container.dashboard


KeysDep = Annotated[KeyStore, Depends(_keys)]
UsageDep = Annotated[UsageStore, Depends(_usage)]
ConfigDep = Annotated[ConfigService, Depends(_config)]
AuthDep = Annotated[AuthService, Depends(_auth)]
QuotaDep = Annotated[QuotaService, Depends(_quota)]
CatalogDep = Annotated[ModelCatalog, Depends(_catalog)]
RecorderDep = Annotated[UsageRecorder, Depends(_recorder)]
ProxyDep = Annotated[ProxyService, Depends(_proxy)]
DashboardDep = Annotated[DashboardService, Depends(_dashboard)]


def require_admin(
    config: ConfigDep,
    authorization: Annotated[str | None, Header()] = None,
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    """管理端令牌校验。``OPENPROXY_ADMIN_TOKEN`` 未设置时**完全放行**。

    放行是默认行为，因为本站默认绑 127.0.0.1；一旦把它暴露到局域网就必须设置
    令牌 —— 所以 README 与「渠道」页都会显式提示这一点，而不是假装默认就安全。
    """
    expected = config.snapshot.admin_token
    if not expected:
        return
    supplied = (x_admin_token or "").strip()
    if not supplied and authorization:
        raw = authorization.strip()
        supplied = raw[len("bearer ") :] if raw.lower().startswith("bearer ") else raw
    if not supplied:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少管理令牌：请在 X-Admin-Token 头里提供 OPENPROXY_ADMIN_TOKEN",
        )
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理令牌不正确")


AdminDep = Depends(require_admin)
