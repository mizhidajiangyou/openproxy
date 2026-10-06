"""探测结果的持久化。

**为什么落在``kv`` 表而不是新建一张表**：探测结果每个模型就一行、总共十行，
是一次性读写的整块JSON。为它加一张表 + 一次schema 迁移，换来的是「查询单个模型的
历史探测」这个本站根本不提供的功能。``kv`` 已经是跨重启的配置存储（覆盖层用它），
复用同一条路径就没有第二个真相来源。

**为什么必须落库**：``/v1/models`` 只回 ``id/created/owned_by``，查不出
``FreeTierError`` —— 站外能不能调通，只有真发一次请求才知道（实测2026-10-04：
10 个免费模型里9 个回403 ``can only be used from within OpenCode``）。
而这个结论不能只留在内存里：进程重启后控制台又变回「未探测」，用户会以为
「刚才还能用」是自己看错了。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Final

from openproxy.store.db import Database

PROBE_KEY: Final = "model_reachability"
"""``kv`` 里的键名。改它等于丢弃全部历史探测结果。"""

#: 单条记录的上限。防御畸形数据把 ``kv`` 撑成几 MB（它没有大小约束）。
MAX_DETAIL_CHARS: Final = 300


@dataclass(frozen=True, slots=True)
class Reachability:
    """一个模型的站外可达性。"""

    status: str
    """``ok`` / ``blocked`` / ``unknown``。

    * ``ok`` —— 真发了一次最小请求，上游返回 2xx。
    * ``blocked`` —— 上游明确拒绝（实测是 ``403 FreeTierError``）。
    * ``unknown`` —— 没探测过，或探测本身失败（网络问题不能算成「不可用」）。
    """
    checked_at: int = 0
    detail: str = ""
    status_code: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def to_public(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: str) -> Reachability:
        try:
            payload: Any = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return cls(status="unknown", detail="探测记录损坏")
        if not isinstance(payload, dict):
            return cls(status="unknown", detail="探测记录格式错误")
        status = payload.get("status")
        if status not in {"ok", "blocked", "unknown"}:
            return cls(status="unknown", detail="未知的探测状态")
        return cls(
            status=str(status),
            checked_at=int(payload.get("checked_at") or 0),
            detail=str(payload.get("detail") or "")[:MAX_DETAIL_CHARS],
            status_code=int(payload.get("status_code") or 0),
        )


class ProbeStore:
    """``{model_id: Reachability}`` 的整块读写。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def load(self) -> dict[str, Reachability]:
        raw = self._db.kv_get(PROBE_KEY)
        if not raw:
            return {}
        try:
            payload: Any = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        out: dict[str, Reachability] = {}
        for model_id, item in payload.items():
            if isinstance(model_id, str) and isinstance(item, str):
                out[model_id] = Reachability.from_json(item)
        return out

    def save(self, results: dict[str, Reachability]) -> None:
        payload = {model_id: item.to_json() for model_id, item in results.items()}
        self._db.kv_set(PROBE_KEY, json.dumps(payload, ensure_ascii=False))
