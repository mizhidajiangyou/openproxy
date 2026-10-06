"""控制台后端：``/api/admin/*``。

所有页面数据都从这里拿。刻意分成三个命名空间：

* ``/api/admin/overview|usage|models|channel`` —— 页面读
* ``/api/admin/keys*`` —— 密钥增删改
* ``/api/admin/settings`` / ``/maintenance/*`` —— 运行期设置与运维

密钥明文**只在创建响应里出现一次**，之后任何接口都只回 sha256 前缀。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from openproxy.api.deps import (
    AdminDep,
    AuthDep,
    CatalogDep,
    ConfigDep,
    ContainerDep,
    DashboardDep,
    KeysDep,
    ProxyDep,
    QuotaDep,
    RecorderDep,
    UsageDep,
)
from openproxy.config import ConfigError
from openproxy.domain import FREE_MODELS, UsageFilter
from openproxy.store import DAY_MS, day_start_ms, now_ms

router = APIRouter(prefix="/api/admin", tags=["console"], dependencies=[AdminDep])


async def _offload[T](func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """把同步的 sqlite 聚合丢到线程池。

    控制台的每个读接口都是 ``async def``，但里面全是同步 sqlite 调用。在单 worker
    事件循环上，一次仪表盘加载实测会把循环占住 ~300ms（30 万行时更长，且随行数
    线性增长）—— 也就是说**打开一次控制台会掐住所有正在进行的流式转发**。
    转发的正确性优先于控制台的响应速度，所以读一律走线程池。
    """
    return await asyncio.to_thread(func, *args, **kwargs)


# ------------------------------------------------------------------ 读 ---


@router.get("/overview", summary="总览页数据")
async def overview(
    dashboard: DashboardDep, trend_days: int = Query(14, ge=1, le=90)
) -> dict[str, Any]:
    return await _offload(dashboard.overview, trend_days=trend_days)


@router.get("/usage", summary="调用记录（过滤 + 分页）")
async def usage(
    dashboard: DashboardDep,
    since: int | None = None,
    until: int | None = None,
    model: str | None = None,
    key_id: str | None = None,
    status_filter: str | None = Query(None, alias="status", pattern="^(ok|error)$"),
    search: str = Query("", max_length=200),
    anonymous_only: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    return await _offload(
        dashboard.usage_page,
        UsageFilter(
            since=since,
            until=until,
            model=model,
            key_id=key_id,
            status=status_filter or "",
            search=search,
            anonymous_only=anonymous_only,
            page=page,
            page_size=page_size,
        ),
    )


@router.get("/models", summary="模型页数据")
async def models(dashboard: DashboardDep, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    return await _offload(dashboard.models, days=days)


@router.get("/channel", summary="渠道页数据")
async def channel(
    dashboard: DashboardDep,
    usage: UsageDep,
    config: ConfigDep,
    days: int = Query(14, ge=1, le=90),
) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        payload = dashboard.channel(days=days, upstream_base=config.snapshot.upstream_base)
        today = day_start_ms(now_ms(), usage.tz_offset_minutes)
        rpm, tpm = usage.live_rate()
        payload["summary"] = {**payload["summary"], "rpm": rpm, "tpm": tpm}
        payload["today_window"] = {"start": today, "end": today + DAY_MS}
        return payload

    payload = await _offload(build)
    payload["admin_protected"] = bool(config.snapshot.admin_token)
    return payload


@router.post("/channel/probe", summary="立即探测上游")
async def probe(
    proxy: ProxyDep,
    catalog: CatalogDep,
    reachability: bool = Query(
        default=False,
        description="逐个模型发最小请求验「站外能不能调通」（会消耗上游额度，默认关闭）",
    ),
) -> dict[str, Any]:
    result = await catalog.probe(
        proxy.http_client, proxy.config.upstream_base, reachability=reachability
    )
    return result.to_public()


# -------------------------------------------------------------- 密钥 ---


class KeyCreate(BaseModel):
    name: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=200)
    daily_token_quota: int | None = Field(default=None, ge=1, le=1 << 40)


class KeyPatch(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=200)
    #: ``0`` 表示「清除配额 / 不限」，与全局配额的约定一致。
    #: 之前这里是 ``ge=1``，于是界面上「留空 = 不限」根本提交不上去 ——
    #: ``null`` 被当成「不修改」，配额一旦设过就再也取消不掉。
    daily_token_quota: int | None = Field(default=None, ge=0, le=1 << 40)
    disabled: bool | None = None


@router.get("/keys", summary="密钥列表")
async def list_keys(
    keys: KeysDep, dashboard: DashboardDep, config: ConfigDep
) -> dict[str, Any]:
    rows = keys.list()
    # 窗口跟着 retain_days 走：保留 7 天却按 90 天统计，会让页面显得「有数据」
    # 而记录其实早被清掉了。
    window = min(365, max(1, config.snapshot.retain_days))
    stats = {
        k["key_id"]: k for k in await _offload(dashboard.by_key, days=window)
    }
    items = [
        {
            **row.to_public(),
            "requests": int(stats.get(row.id, {}).get("requests", 0)),
            "total_tokens": int(stats.get(row.id, {}).get("total_tokens", 0)),
        }
        for row in rows
    ]
    return {
        "items": items,
        "total": len(items),
        "active": sum(1 for r in rows if not r.disabled),
        "require_key": config.snapshot.require_key,
        "window_days": window,
    }


@router.post("/keys", status_code=status.HTTP_201_CREATED, summary="签发密钥")
async def create_key(keys: KeysDep, payload: KeyCreate = Body()) -> dict[str, Any]:
    try:
        record, raw = keys.create(
            payload.name, note=payload.note, daily_token_quota=payload.daily_token_quota
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "key": record.to_public(),
        # 明文只在这里出现一次，之后任何接口都拿不到
        "secret": raw,
        "hint": "这是唯一一次显示明文密钥，请立即保存。之后只能看到前缀。",
    }


@router.patch("/keys/{key_id}", summary="改密钥")
async def patch_key(keys: KeysDep, key_id: str, payload: KeyPatch = Body()) -> dict[str, Any]:
    if keys.get(key_id) is None:
        raise HTTPException(status_code=404, detail="密钥不存在")
    try:
        if payload.name is not None:
            keys.rename(key_id, payload.name)
        if payload.daily_token_quota is not None:
            keys.set_quota(key_id, payload.daily_token_quota or None)
        if payload.disabled is not None:
            keys.set_disabled(key_id, payload.disabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    updated = keys.get(key_id)
    if updated is None:  # pragma: no cover — 并发删除
        raise HTTPException(status_code=404, detail="密钥已被删除")
    return updated.to_public()


@router.delete("/keys/{key_id}", summary="删除密钥")
async def delete_key(keys: KeysDep, usage: UsageDep, key_id: str) -> dict[str, Any]:
    """删除密钥。**历史用量保留** —— 报表不能因为一次误删而出现空洞。"""
    kept = usage.list(UsageFilter(key_id=key_id, page=1, page_size=1)).total
    if not keys.delete(key_id):
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"deleted": key_id, "kept_usage_records": kept}


# ---------------------------------------------------------------- 设置 ---


class SettingsPatch(BaseModel):
    """控制台可改的字段。

    字段集合必须与 :data:`openproxy.config.Overlays` **完全一致** ——
    多一个会让控制台出现「点不动」的开关（Pydantic 静默丢弃未知字段），
    少一个会让界面上的开关报「没有需要修改的字段」。
    ``TestSettingsContract`` 会把两边对着比。
    """

    require_key: bool | None = None
    upstream_base: str | None = Field(default=None, max_length=200)
    retain_days: int | None = Field(default=None, ge=1, le=3650)
    daily_token_quota: int | None = Field(default=None, ge=0, le=1 << 40)
    free_models_only: bool | None = None
    inject_stream_usage: bool | None = None
    reasoning_effort: str | None = Field(default=None, max_length=16)
    opencode_models: list[str] | None = Field(default=None, max_length=32)
    """改走本机 opencode 服务的模型 id 列表；``[]`` = 全部直通。

    用 ``list`` 而不是 ``set``：Pydantic 收到 JSON 数组会保序，
    而集合的迭代顺序不可预测 —— 而前端需要用这个顺序渲染勾选列表。
    长度上限 32 是给「所有模型都走 opencode」留的余量（当前清单 10 个），
    同时挡住「把整份模型清单误当数组提交」这类事故。
    """


@router.get("/settings", summary="读设置")
async def read_settings(config: ConfigDep, catalog: CatalogDep) -> dict[str, Any]:
    snapshot = config.snapshot
    return {
        **snapshot.public_dict(),
        "base_url_hint": f"http://{snapshot.host}:{snapshot.port}/v1",
        "free_model_ids": [m.model_id for m in FREE_MODELS],
        "probe": catalog.last_probe.to_public() if catalog.last_probe else None,
    }


@router.patch("/settings", summary="改设置")
async def patch_settings(config: ConfigDep, payload: SettingsPatch = Body()) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    try:
        config.patch(**changes)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return config.snapshot.public_dict()


@router.post("/settings/reset", summary="恢复环境变量基线")
async def reset_settings(config: ConfigDep) -> dict[str, Any]:
    return config.reset_overlays().public_dict()


@router.get("/client-context", summary="管理端自身的配额视角")
async def client_context(
    request: Request, auth: AuthDep, quota: QuotaDep, config: ConfigDep
) -> dict[str, Any]:
    """用管理端请求头里的密钥解析配额，让「设置」页能显示「我今天用了多少」。"""
    client = auth.resolve(request.headers)
    return quota.snapshot(client, config.snapshot)


# -------------------------------------------------------------- 运维 ---


@router.post("/maintenance/prune", summary="立即清理过期用量")
async def prune(container: ContainerDep) -> dict[str, Any]:
    removed = await container.prune_now()
    return {"removed": removed, "retain_days": container.config.snapshot.retain_days}


@router.post("/maintenance/flush", summary="排空用量写入队列")
async def flush(recorder: RecorderDep) -> dict[str, Any]:
    ok = recorder.flush(timeout=5.0)
    return {"flushed": ok, "pending": recorder.pending, "written": recorder.written,
            "dropped": recorder.dropped, "failed": recorder.failed}
