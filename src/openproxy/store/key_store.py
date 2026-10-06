"""密钥仓储。

密钥明文**只在签发那一刻返回一次**，库里只留 sha256 的前 32 位十六进制。
查找用哈希做主键，所以鉴权路径是 O(1) 的等值查询，不需要扫库比对。

生成用 :mod:`secrets`，前缀固定成 ``sk-op-`` 以便人工识别本站签发的密钥
（和 OpenAI 风格的 ``sk-`` 区分开，避免用户把它当成上游密钥填到别处）。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time

from openproxy.domain import ApiKey
from openproxy.store.db import Database

KEY_PREFIX = "sk-op-"
_SECRET_BYTES = 24  # 24 字节 = 192 bit 熵，远超可暴力枚举的范围
KEY_ID_LENGTH = 32  # sha256 十六进制的前 32 个字符 = 128 bit
PREVIEW_LENGTH = 11  # "sk-op-" + 前 5 位密钥字符，用于界面辨认


def hash_key(raw_key: str) -> str:
    """明文密钥 → 存储主键。

    不可逆；比较走哈希等值，不泄露原文，本地单用户场景也不需要常量时间。
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:KEY_ID_LENGTH]


def new_key_material() -> tuple[str, str, str]:
    """生成 ``(明文密钥, 主键, 展示前缀)``。明文只在返回值里出现一次。"""
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    raw = f"{KEY_PREFIX}{secret}"
    return raw, hash_key(raw), raw[:PREVIEW_LENGTH]


class KeyStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        name: str,
        *,
        note: str = "",
        daily_token_quota: int | None = None,
        created_at: int | None = None,
    ) -> tuple[ApiKey, str]:
        """建一张密钥。返回 ``(记录, 明文)``，明文请立即展示给用户。"""
        clean_name = name.strip() or "未命名密钥"
        if len(clean_name) > 64:
            raise ValueError("name 超过 64 个字符")
        if daily_token_quota is not None and daily_token_quota <= 0:
            raise ValueError("daily_token_quota 必须为正数或 None")

        raw, key_id, preview = new_key_material()
        now = int(created_at if created_at is not None else time.time() * 1000)
        with self._db.write() as conn:
            conn.execute(
                """
                INSERT INTO api_keys
                    (id, name, prefix, created_at, disabled_at,
                     daily_token_quota, note, last_used_at)
                VALUES (?,?,?,?,NULL,?,?,NULL)
                """,
                (key_id, clean_name, preview, now, daily_token_quota, note.strip()[:200]),
            )
        record = ApiKey(
            id=key_id,
            name=clean_name,
            prefix=preview,
            created_at=now,
            note=note.strip()[:200],
            daily_token_quota=daily_token_quota,
        )
        return record, raw

    def get(self, key_id: str) -> ApiKey | None:
        row = self._db.connection.execute(
            "SELECT * FROM api_keys WHERE id = ?", (key_id,)
        ).fetchone()
        return None if row is None else _row_to_key(row)

    def resolve(self, raw_key: str) -> ApiKey | None:
        """按明文查密钥。空串 / 非本站前缀直接返回 ``None``，不做哈希。"""
        candidate = raw_key.strip()
        if not candidate.startswith(KEY_PREFIX):
            return None
        return self.get(hash_key(candidate))

    def list(self, *, include_disabled: bool = True) -> tuple[ApiKey, ...]:
        sql = "SELECT * FROM api_keys"
        if not include_disabled:
            sql += " WHERE disabled_at IS NULL"
        sql += " ORDER BY created_at DESC, id"
        rows = self._db.connection.execute(sql).fetchall()
        return tuple(_row_to_key(r) for r in rows)

    def set_disabled(self, key_id: str, disabled: bool, *, at: int | None = None) -> bool:
        """禁用 / 启用。返回是否命中。启用时把 ``disabled_at`` 清成 NULL。"""
        stamp = int(at if at is not None else time.time() * 1000)
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET disabled_at = ? WHERE id = ?",
                (stamp if disabled else None, key_id),
            )
            return bool(cur.rowcount)

    def delete(self, key_id: str) -> bool:
        """删除密钥。**用量记录不受影响** —— 历史必须留得住。"""
        with self._db.write() as conn:
            cur = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            return bool(cur.rowcount)

    def rename(self, key_id: str, name: str) -> bool:
        clean = name.strip()
        if not clean or len(clean) > 64:
            raise ValueError("name 需为 1–64 个字符")
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET name = ? WHERE id = ?", (clean, key_id)
            )
            return bool(cur.rowcount)

    def set_quota(self, key_id: str, quota: int | None) -> bool:
        if quota is not None and quota <= 0:
            raise ValueError("quota 必须为正数或 None")
        with self._db.write() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET daily_token_quota = ? WHERE id = ?", (quota, key_id)
            )
            return bool(cur.rowcount)

    def count(self) -> int:
        row = self._db.connection.execute("SELECT COUNT(*) AS c FROM api_keys").fetchone()
        return int(row["c"])


def _row_to_key(row: sqlite3.Row) -> ApiKey:
    return ApiKey(
        id=str(row["id"]),
        name=str(row["name"]),
        prefix=str(row["prefix"]),
        created_at=int(row["created_at"]),
        note=str(row["note"]),
        disabled_at=None if row["disabled_at"] is None else int(row["disabled_at"]),
        daily_token_quota=(
            None
            if row["daily_token_quota"] is None
            else int(row["daily_token_quota"])
        ),
        last_used_at=None if row["last_used_at"] is None else int(row["last_used_at"]),
    )
