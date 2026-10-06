"""用量仓储：写入、过滤分页、按天/按模型/按密钥聚合、惰性清理。

时间约定
--------
所有 ``ts`` 都是 **UTC epoch 毫秒**。按天聚合需要一个 UTC 偏移，
由构造参数 ``tz_offset_minutes`` 固定（默认取本机当前偏移），并被翻译成
SQLite 的 ``date()`` 修饰符字符串，例如 ``-480 minutes``。这样：

* 单元测试可以钉死偏移量（默认 +480）→ 结果确定，不随运行机器的时区变化；
* 生产环境取本机时区 → 「今天」对用户是本地日，符合直觉。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from openproxy.domain import (
    ANONYMOUS_LABEL,
    ErrorKind,
    KeyStat,
    ModelStat,
    StatusPoint,
    Summary,
    TokenUsage,
    TrendPoint,
    UsageFilter,
    UsagePage,
    UsageRecord,
)
from openproxy.store.db import Database

DAY_MS = 86_400_000
MINUTE_MS = 60_000
MAX_TZ_OFFSET_MINUTES = 14 * 60


def now_ms() -> int:
    return int(time.time() * 1000)


def local_tz_offset_minutes() -> int:
    """本机当前 UTC 偏移（分钟）。刷新一次即可 —— 进程生命周期内偏移不会突变。"""
    offset = datetime.now().astimezone().utcoffset()
    return 0 if offset is None else int(offset.total_seconds() // 60)


def tz_modifier_for(offset_minutes: int) -> str:
    """把 UTC 偏移翻译成 ``date()`` 可用的修饰符。"""
    if not -MAX_TZ_OFFSET_MINUTES <= offset_minutes <= MAX_TZ_OFFSET_MINUTES:
        raise ValueError(f"时区偏移超出范围: {offset_minutes}")
    return f"{offset_minutes:+d} minutes"


def day_start_ms(ts_ms: int, offset_minutes: int) -> int:
    """给定时刻所在「本地日」的 UTC 毫秒起点。"""
    local = datetime.fromtimestamp(ts_ms / 1000, tz=UTC) + timedelta(minutes=offset_minutes)
    local_day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    back = local_day - timedelta(minutes=offset_minutes)
    return int(back.timestamp() * 1000)


def day_label(ts_ms: int, offset_minutes: int) -> str:
    local = datetime.fromtimestamp(ts_ms / 1000, tz=UTC) + timedelta(minutes=offset_minutes)
    return local.strftime("%Y-%m-%d")


class UsageStore:
    """所有用量查询的入口。无状态（除 ``Database`` 外），可安全并发调用。"""

    def __init__(self, db: Database, *, tz_offset_minutes: int | None = None) -> None:
        self._db = db
        self.tz_offset_minutes = (
            local_tz_offset_minutes() if tz_offset_minutes is None else int(tz_offset_minutes)
        )
        self._tz_mod = tz_modifier_for(self.tz_offset_minutes)

    @property
    def db(self) -> Database:
        """底层连接。暴露成只读属性是为了让「包装一个已有 store」的测试夹具
        不必去摸 ``_db`` 私有字段。"""
        return self._db

    # ------------------------------------------------------------- 写入 ---

    def record(self, rec: UsageRecord) -> None:
        """落一条用量。**只写计数** —— 没有任何 prompt / completion 字段。"""
        u = rec.usage
        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO usage_records (
                    ts, model, path, stream, status, latency_ms,
                    prompt_tokens, completion_tokens, cached_tokens,
                    reasoning_tokens, total_tokens, usage_known,
                    bytes_in, bytes_out, error_kind,
                    key_id, key_label, anonymous, client_ip
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rec.ts,
                    rec.model,
                    rec.path,
                    int(rec.stream),
                    rec.status,
                    rec.latency_ms,
                    u.prompt_tokens,
                    u.completion_tokens,
                    u.cached_tokens,
                    u.reasoning_tokens,
                    u.total_tokens,
                    int(u.known),
                    rec.bytes_in,
                    rec.bytes_out,
                    str(rec.error_kind),
                    rec.key_id,
                    rec.key_label,
                    int(rec.anonymous),
                    rec.client_ip,
                ),
            )

    def prune(self, before_ts: int) -> int:
        """删掉 ``before_ts`` 之前的记录，返回删除行数。"""
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM usage_records WHERE ts < ?", (before_ts,))
            return int(cur.rowcount or 0)

    def touch_key(self, key_id: str, ts: int) -> None:
        with self._db.write() as conn:
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (ts, key_id))

    # ------------------------------------------------------------- 读取 ---

    def list(self, flt: UsageFilter) -> UsagePage:
        f = flt.normalized()
        where, params = _filter_sql(f)
        count_row = self._db.connection.execute(
            f"SELECT COUNT(*) AS c FROM usage_records{where}", params
        ).fetchone()
        total = int(count_row["c"])
        rows = self._db.connection.execute(
            f"SELECT * FROM usage_records{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*params, f.page_size, f.offset()],
        ).fetchall()
        return UsagePage(
            items=tuple(_row_to_record(r) for r in rows),
            total=total,
            page=f.page,
            page_size=f.page_size,
        )

    def summary(self, since: int | None = None, until: int | None = None) -> Summary:
        where, params = _where(*_range_clauses(since, until))
        row = self._db.connection.execute(
            f"""
            SELECT
                COUNT(*)                                        AS requests,
                COALESCE(SUM(CASE WHEN status < 400 THEN 0 ELSE 1 END), 0) AS errors,
                COALESCE(SUM(prompt_tokens), 0)                 AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0)             AS completion_tokens,
                COALESCE(SUM(cached_tokens), 0)                 AS cached_tokens,
                COALESCE(SUM(reasoning_tokens), 0)              AS reasoning_tokens,
                COALESCE(SUM(total_tokens), 0)                  AS total_tokens,
                COALESCE(SUM(CASE WHEN usage_known = 0 THEN 1 ELSE 0 END), 0) AS unknown_usage,
                COALESCE(SUM(bytes_out), 0)                     AS bytes_out,
                COALESCE(AVG(latency_ms), 0)                    AS avg_latency
            FROM usage_records{where}
            """,
            params,
        ).fetchone()
        requests = int(row["requests"])
        avg = float(row["avg_latency"] or 0.0)
        return Summary(
            requests=requests,
            errors=int(row["errors"]),
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            cached_tokens=int(row["cached_tokens"]),
            reasoning_tokens=int(row["reasoning_tokens"]),
            total_tokens=int(row["total_tokens"]),
            unknown_usage=int(row["unknown_usage"]),
            avg_latency_ms=round(avg),
            bytes_out=int(row["bytes_out"]),
        )

    def tokens_between(self, since: int, until: int, key_id: str | None = None) -> int:
        """区间内消耗的 token，配额判定用。``key_id=None`` 表示全体合计。"""
        sql = (
            "SELECT COALESCE(SUM(total_tokens), 0) AS t "
            "FROM usage_records WHERE ts >= ? AND ts < ?"
        )
        params: list[object] = [since, until]
        if key_id is not None:
            sql += " AND key_id = ?"
            params.append(key_id)
        return int(self._db.connection.execute(sql, params).fetchone()["t"])

    def live_rate(self, window_ms: int = MINUTE_MS, now: int | None = None) -> tuple[int, int]:
        """最近一个窗口内的 (每分钟请求数, 每分钟 token 数)。窗口内无数据则返回 (0, 0)。"""
        end = now if now is not None else now_ms()
        row = self._db.connection.execute(
            """
            SELECT COUNT(*) AS c, COALESCE(SUM(total_tokens), 0) AS t
            FROM usage_records WHERE ts >= ?
            """,
            (end - window_ms,),
        ).fetchone()
        return int(row["c"]), int(row["t"])

    def trend(self, days: int = 14, until: int | None = None) -> tuple[TrendPoint, ...]:
        """最近 ``days`` 个本地日的每日用量，**缺失的日期补零**。

        补零是必须的：图表遇到空洞会画成断线，而「那天没人调用」本身就是信息。
        """
        days = max(1, min(days, 365))
        end = until if until is not None else now_ms()
        last_day = day_start_ms(end, self.tz_offset_minutes)
        first_day = last_day - (days - 1) * DAY_MS

        rows = self._db.connection.execute(
            """
            SELECT
                date(ts / 1000, 'unixepoch', ?) AS bucket,
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0)      AS total_tokens
            FROM usage_records
            WHERE ts >= ? AND ts < ?
            GROUP BY bucket
            """,
            (self._tz_mod, first_day, last_day + DAY_MS),
        ).fetchall()
        by_bucket = {str(r["bucket"]): r for r in rows}

        out: list[TrendPoint] = []
        for i in range(days):
            day_ts = first_day + i * DAY_MS
            label = day_label(day_ts, self.tz_offset_minutes)
            row = by_bucket.get(label)
            out.append(
                TrendPoint(
                    bucket=label,
                    requests=int(row["requests"]) if row else 0,
                    prompt_tokens=int(row["prompt_tokens"]) if row else 0,
                    completion_tokens=int(row["completion_tokens"]) if row else 0,
                    total_tokens=int(row["total_tokens"]) if row else 0,
                )
            )
        return tuple(out)

    def by_model(
        self, since: int | None = None, until: int | None = None, limit: int = 50
    ) -> tuple[ModelStat, ...]:
        where, params = _where(*_range_clauses(since, until))
        rows = self._db.connection.execute(
            f"""
            SELECT model,
                   COUNT(*)                                    AS requests,
                   COALESCE(SUM(total_tokens), 0)              AS total_tokens,
                   COALESCE(SUM(prompt_tokens), 0)             AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0)         AS completion_tokens,
                   COALESCE(SUM(CASE WHEN status < 400 THEN 0 ELSE 1 END), 0) AS errors,
                   COALESCE(AVG(latency_ms), 0)                AS avg_latency
            FROM usage_records{where}
            GROUP BY model
            ORDER BY total_tokens DESC, requests DESC
            LIMIT ?
            """,
            [*params, max(1, limit)],
        ).fetchall()
        return tuple(
            ModelStat(
                model=str(r["model"]),
                requests=int(r["requests"]),
                total_tokens=int(r["total_tokens"]),
                prompt_tokens=int(r["prompt_tokens"]),
                completion_tokens=int(r["completion_tokens"]),
                errors=int(r["errors"]),
                avg_latency_ms=round(float(r["avg_latency"] or 0.0)),
            )
            for r in rows
        )

    def by_key(self, since: int | None = None, until: int | None = None) -> tuple[KeyStat, ...]:
        where, params = _where(*_range_clauses(since, until))
        agg = self._db.connection.execute(
            f"""
            SELECT COALESCE(key_id, '')           AS key_id,
                   MAX(anonymous)                 AS anonymous,
                   COUNT(*)                        AS requests,
                   COALESCE(SUM(total_tokens), 0)  AS total_tokens
            FROM usage_records{where}
            GROUP BY key_id
            ORDER BY total_tokens DESC, requests DESC
            """,
            params,
        ).fetchall()

        # 名称单独取「最近一条记录」的那个，而不是 GROUP BY 里随便一行：
        # ``key_label`` 是每条记录当时的名称快照（刻意如此，改名后历史仍显示旧名），
        # 而密钥一旦改名，``GROUP BY key_id, label`` 会把它拆成「旧名」「新名」两桶。
        # 调用方按 key_id 建字典、只保留最后一行，于是用量被少报 —— 实测改名一次后
        # 密钥页显示 1 次调用，而该 key_id 的记录实际有 3 条。
        labels: dict[str, str] = {}
        if agg:
            newest = self._db.connection.execute(
                f"""
                SELECT COALESCE(u.key_id, '') AS key_id, u.key_label AS key_label
                FROM usage_records u
                JOIN (
                    SELECT COALESCE(key_id, '') AS key_id, MAX(ts) AS mx
                    FROM usage_records{where}
                    GROUP BY key_id
                ) t ON COALESCE(u.key_id, '') = t.key_id AND u.ts = t.mx
                -- 输出列与 GROUP BY 都要 COALESCE：聚合那侧匿名桶的键是 ''，
                -- 这里原样返回 NULL 会变成字符串 'None'，两边永远对不上 ——
                -- 匿名桶的名字只是靠下游的 ``or ANONYMOUS_LABEL`` 兜底才碰巧正确。
                GROUP BY COALESCE(u.key_id, '')
                """,
                params,
            ).fetchall()
            labels = {str(r["key_id"]): str(r["key_label"] or "") for r in newest}

        return tuple(
            KeyStat(
                key_id=str(r["key_id"]),
                label=labels.get(str(r["key_id"])) or ANONYMOUS_LABEL,
                anonymous=bool(r["anonymous"]),
                requests=int(r["requests"]),
                total_tokens=int(r["total_tokens"]),
            )
            for r in agg
        )

    def status_trend(self, days: int = 14, until: int | None = None) -> tuple[StatusPoint, ...]:
        """最近 ``days`` 天的成功/失败分布，缺失日期补零。"""
        days = max(1, min(days, 365))
        end = until if until is not None else now_ms()
        last_day = day_start_ms(end, self.tz_offset_minutes)
        first_day = last_day - (days - 1) * DAY_MS
        rows = self._db.connection.execute(
            """
            SELECT date(ts / 1000, 'unixepoch', ?) AS bucket,
                   COALESCE(SUM(CASE WHEN status < 400 THEN 1 ELSE 0 END), 0) AS ok,
                   COALESCE(SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END), 0) AS err
            FROM usage_records WHERE ts >= ? AND ts < ? GROUP BY bucket
            """,
            (self._tz_mod, first_day, last_day + DAY_MS),
        ).fetchall()
        by_bucket = {str(r["bucket"]): r for r in rows}
        out: list[StatusPoint] = []
        for i in range(days):
            day_ts = first_day + i * DAY_MS
            label = day_label(day_ts, self.tz_offset_minutes)
            row = by_bucket.get(label)
            out.append(
                StatusPoint(
                    bucket=label,
                    ok=int(row["ok"]) if row else 0,
                    error=int(row["err"]) if row else 0,
                )
            )
        return tuple(out)

    def error_kinds(self, since: int | None = None, until: int | None = None) -> dict[str, int]:
        clauses, params = _range_clauses(since, until)
        clauses.append("error_kind <> ''")
        where, params = _where(clauses, params)
        rows = self._db.connection.execute(
            f"SELECT error_kind, COUNT(*) AS c FROM usage_records{where} "
            "GROUP BY error_kind ORDER BY c DESC",
            params,
        ).fetchall()
        return {str(r["error_kind"]): int(r["c"]) for r in rows}

    def distinct_models(self) -> tuple[str, ...]:
        rows = self._db.connection.execute(
            "SELECT DISTINCT model FROM usage_records ORDER BY model"
        ).fetchall()
        return tuple(str(r["model"]) for r in rows)


# ------------------------------------------------------------------ 内部 ---


def _range_clauses(since: int | None, until: int | None) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts < ?")
        params.append(until)
    return clauses, params


def _where(clauses: Sequence[str], params: Sequence[object]) -> tuple[str, list[object]]:
    """把子句列表拼成 WHERE 片段。空列表返回空串（不是 ``" WHERE "``）。"""
    if not clauses:
        return "", list(params)
    return " WHERE " + " AND ".join(clauses), list(params)


def _filter_sql(f: UsageFilter) -> tuple[str, list[object]]:
    clauses, params = _range_clauses(f.since, f.until)

    if f.model:
        clauses.append("model = ?")
        params.append(f.model)
    if f.key_id:
        clauses.append("key_id = ?")
        params.append(f.key_id)
    if f.anonymous_only:
        clauses.append("anonymous = 1")
    if f.status == "ok":
        clauses.append("status < 400")
    elif f.status == "error":
        clauses.append("status >= 400")
    if f.search:
        # 只在非敏感列上模糊匹配：模型名、路径、错误类别、密钥名。**永不**匹配请求正文
        # （正文根本不落库），也不匹配密钥明文（库里只有 sha256 前缀）。
        escaped = _escape_like(f.search)
        clauses.append(
            "(model LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' "
            "OR error_kind LIKE ? ESCAPE '\\' OR key_label LIKE ? ESCAPE '\\')"
        )
        params.extend([f"%{escaped}%"] * 4)
    return _where(clauses, params)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_record(row: sqlite3.Row) -> UsageRecord:
    usage = TokenUsage(
        prompt_tokens=int(row["prompt_tokens"]),
        completion_tokens=int(row["completion_tokens"]),
        cached_tokens=int(row["cached_tokens"]),
        reasoning_tokens=int(row["reasoning_tokens"]),
        total_tokens=int(row["total_tokens"]),
        known=bool(row["usage_known"]),
    )
    try:
        kind = ErrorKind(str(row["error_kind"]))
    except ValueError:
        kind = ErrorKind.INTERNAL
    return UsageRecord(
        ts=int(row["ts"]),
        model=str(row["model"]),
        path=str(row["path"]),
        stream=bool(row["stream"]),
        status=int(row["status"]),
        latency_ms=int(row["latency_ms"]),
        usage=usage,
        key_id=row["key_id"],
        key_label=str(row["key_label"]),
        anonymous=bool(row["anonymous"]),
        bytes_in=int(row["bytes_in"]),
        bytes_out=int(row["bytes_out"]),
        error_kind=kind,
        client_ip=str(row["client_ip"]),
    )
