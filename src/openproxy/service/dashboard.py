"""仪表盘 / 调用记录 / 模型 / 渠道 四个页面的数据组装。

**一个数据集，多个渲染器**（R21）：所有页面读的都是同一批 :class:`UsageStore`
聚合方法，这里只负责把它们拼成前端要的 JSON 形状。数字只在这里算一次，
前端不再做任何二次聚合 —— 否则总览页的「今日请求」和记录页的分页总数
一旦口径不一致，就会出现两个都「看起来对」但互相矛盾的数字。
"""

from __future__ import annotations

from typing import Any

from openproxy.domain import Summary, UsageFilter, UsagePage, UsageRecord
from openproxy.service.model_catalog import ModelCatalog, ProbeResult
from openproxy.store import DAY_MS, UsageStore, day_start_ms, now_ms


class DashboardService:
    def __init__(self, usage: UsageStore, catalog: ModelCatalog) -> None:
        self._usage = usage
        self._catalog = catalog

    # ------------------------------------------------------------ 时间窗 ---

    def _window(self, days: int) -> tuple[int, int]:
        """最近 ``days`` 个本地自然日的半开区间 ``[start, end)``。"""
        days = max(1, min(days, 365))
        now = now_ms()
        today = day_start_ms(now, self._usage.tz_offset_minutes)
        start = today - (days - 1) * DAY_MS
        return start, today + DAY_MS

    # ------------------------------------------------------------ 总览 ---

    def overview(self, *, trend_days: int = 14) -> dict[str, Any]:
        day_start, day_end = self._window(1)
        week_start, _ = self._window(7)
        today = self._usage.summary(day_start, day_end)
        week = self._usage.summary(week_start, day_end)
        total = self._usage.summary()
        rpm, tpm = self._usage.live_rate()
        models = self._usage.by_model(day_start, day_end)

        return {
            "today": _summary_payload(today, rpm=0, tpm=0),
            "week": _summary_payload(week, rpm=0, tpm=0),
            "total": _summary_payload(total, rpm=rpm, tpm=tpm),
            "trend": [p.to_public() for p in self._usage.trend(trend_days)],
            "models": [m.to_public() for m in models],
            "recent": [_record_payload(r) for r in self._usage.list(
                UsageFilter(page=1, page_size=8)
            ).items],
            "unknown_usage": total.unknown_usage,
        }

    # -------------------------------------------------------- 调用记录 ---

    def usage_page(self, flt: UsageFilter) -> dict[str, Any]:
        page: UsagePage = self._usage.list(flt)
        f = flt.normalized()
        return {
            "items": [_record_payload(r) for r in page.items],
            "total": page.total,
            "page": page.page,
            "page_size": page.page_size,
            "pages": page.pages,
            "models": list(self._usage.distinct_models()),
            "filters": {
                "model": f.model,
                "key_id": f.key_id,
                "status": f.status,
                "search": f.search,
                "anonymous_only": f.anonymous_only,
                "since": f.since,
                "until": f.until,
            },
        }

    # ------------------------------------------------------------ 模型 ---

    def models(self, *, days: int = 30) -> dict[str, Any]:
        since, until = self._window(days)
        stats = {m.model: m.to_public() for m in self._usage.by_model(since, until, limit=200)}
        entries = self._catalog.entries(stats)
        return {
            "items": [e.to_public() for e in entries],
            "unlisted": [
                m.to_public()
                for m in self._usage.by_model(since, until, limit=200)
                if m.model not in {e.model.model_id for e in entries}
            ],
            "window_days": days,
            "probe": self._catalog.last_probe.to_public() if self._catalog.last_probe else None,
        }

    # ------------------------------------------------------------ 渠道 ---

    def channel(self, *, days: int = 14, upstream_base: str = "") -> dict[str, Any]:
        since, until = self._window(days)
        summary = self._usage.summary(since, until)
        probe: ProbeResult | None = self._catalog.last_probe
        return {
            "upstream_base": upstream_base,
            "window_days": days,
            "summary": _summary_payload(summary, rpm=0, tpm=0),
            "status_trend": [p.to_public() for p in self._usage.status_trend(days)],
            "error_kinds": self._usage.error_kinds(since, until),
            "probe": probe.to_public() if probe else None,
        }

    # -------------------------------------------------------- 按密钥归集 ---

    def by_key(self, *, days: int = 30) -> list[dict[str, Any]]:
        since, until = self._window(days)
        return [k.to_public() for k in self._usage.by_key(since, until)]


# ------------------------------------------------------------------ 工具 ---


def _summary_payload(summary: Summary, *, rpm: int, tpm: int) -> dict[str, Any]:
    payload = summary.to_public()
    payload["rpm"] = rpm
    payload["tpm"] = tpm
    return payload


def _record_payload(record: UsageRecord) -> dict[str, Any]:
    usage = record.usage
    return {
        "ts": record.ts,
        "model": record.model,
        "path": record.path,
        "stream": record.stream,
        "status": record.status,
        "ok": record.status < 400,
        "latency_ms": record.latency_ms,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cached_tokens": usage.cached_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
        "usage_known": usage.known,
        "bytes_out": record.bytes_out,
        "error_kind": str(record.error_kind),
        "key_id": record.key_id,
        "key_label": record.key_label,
        "anonymous": record.anonymous,
        "client_ip": record.client_ip,
    }
