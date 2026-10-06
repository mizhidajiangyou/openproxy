"""SQLite 连接管理与 schema 迁移。

并发模型（与 R05 对齐）：

* **连接**：每线程一个连接（``sqlite3`` 连接不跨线程共享），由
  :class:`Database` 用 ``threading.local`` 持有。httpx 的回调跑在
  ``asyncio.to_thread`` 派生的线程池里，所以连接必须线程局部。
  ``sqlite3.connect(check_same_thread=False)`` 是为了让 :meth:`Database.close`
  能跨线程关干净 —— 「一条连接只被一个线程用」由 ``threading.local`` 保证，
  不依赖 sqlite3 那道检查（Python 3.14 会为未关闭的连接发 ``ResourceWarning``，
  本项目 ``filterwarnings = error``，不关干净就是测试失败）。
* **写**：全部经过 :meth:`Database.write` 里的 ``threading.Lock``。SQLite 本身能
  靠 ``BEGIN IMMEDIATE`` 串行化写者，但那是「靠数据库锁」，失败时表现为
  ``database is locked``；用显式锁则锁竞争发生在进程内、错误信息可控，且
  WAL 下读不阻塞。
* **读**：直接拿本线程连接，不加锁（WAL 允许并发读）。

schema 版本只增不改：:func:`Database.migrate` 按版本号顺序执行。
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

#: 按版本号顺序执行的 DDL。每个版本是一组**独立语句**而不是一段脚本：
#: ``executescript()`` 会隐式提交当前事务，放在 :meth:`Database.write` 里会让
#: 末尾的 COMMIT 报 "cannot commit - no transaction is active"。拆成语句才能保证
#: 「一个版本要么全建成要么全没建」。
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE api_keys (
            id                TEXT PRIMARY KEY,
            name              TEXT NOT NULL,
            prefix            TEXT NOT NULL,
            created_at        INTEGER NOT NULL,
            disabled_at       INTEGER,
            daily_token_quota INTEGER,
            note              TEXT NOT NULL DEFAULT '',
            last_used_at      INTEGER
        )
        """,
        # 刻意不加外键到 api_keys：删密钥后历史用量必须保留（并靠 key_label
        # 快照留住当时的名称），否则「这个客户上周花了多少」会随一次误删消失。
        """
        CREATE TABLE usage_records (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                INTEGER NOT NULL,
            model             TEXT NOT NULL,
            path              TEXT NOT NULL,
            stream            INTEGER NOT NULL DEFAULT 0,
            status            INTEGER NOT NULL,
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens     INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
            total_tokens      INTEGER NOT NULL DEFAULT 0,
            usage_known       INTEGER NOT NULL DEFAULT 0,
            bytes_in          INTEGER NOT NULL DEFAULT 0,
            bytes_out         INTEGER NOT NULL DEFAULT 0,
            error_kind        TEXT NOT NULL DEFAULT '',
            key_id            TEXT,
            key_label         TEXT NOT NULL DEFAULT '',
            anonymous         INTEGER NOT NULL DEFAULT 1,
            client_ip         TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX idx_usage_ts        ON usage_records (ts)",
        "CREATE INDEX idx_usage_model     ON usage_records (model)",
        "CREATE INDEX idx_usage_key_ts    ON usage_records (key_id, ts)",
        "CREATE INDEX idx_usage_status_ts ON usage_records (status, ts)",
        "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ),
)


class Database:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        #: 建过的连接都在这里登记。``threading.local`` **无法枚举**，所以只靠
        #: ``self._local`` 的话，:meth:`close` 只能关掉调用线程那一条 ——
        #: 写入线程和 ``asyncio.to_thread`` 池里那些连接会活到解释器退出。
        #: 线程数是有界的，所以登记全部连接的成本可以忽略。
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        #: 每次 :meth:`close` 自增。各线程用它判断「我手上这条是不是上一代的」。
        self._generation = 0

    # ------------------------------------------------------------- 连接 ---

    def _new_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,  # 自己管事务，避免隐式 BEGIN 打乱读路径
            # False = 允许从别的线程 close 连接。**这不是放开并发保护**：本类靠
            # ``threading.local`` 保证「一条连接只被一个线程用」，
            # ``check_same_thread`` 防的正是这件事，而我们本来就在做。
            #
            # 换成 True 的话，:meth:`close` 只能关掉调用线程自己那一条，写线程与
            # ``asyncio.to_thread`` 池里那些永远关不掉 —— 而 **Python 3.14 的
            # ``sqlite3`` 会为每个未关闭的连接发 ``ResourceWarning``**，在本项目
            # ``filterwarnings = error`` 下直接变成测试失败（实测 8 failed / 14 error）。
            # 也就是说 3.12 只是把这件事藏起来了，不是它不存在。
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if getattr(self._local, "generation", 0) != self._generation:
            # 本线程的连接属于上一代（``close()`` 之后又来取用了）：先关掉再新建。
            # 正常路径下 :meth:`close` 已经把登记的连接全关了，所以这里通常是
            # 「没有可关的」—— 但 ``reload`` 之类只推进 generation 的场景还会走到。
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
                with self._conns_lock:
                    if conn in self._all_conns:
                        self._all_conns.remove(conn)
            self._local.conn = None
            self._local.generation = self._generation
            conn = None
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
            self._local.generation = self._generation
        return conn

    @property
    def open_connections(self) -> int:
        """当前登记的连接数。给测试与 ``/api/health`` 断言用。"""
        with self._conns_lock:
            return len(self._all_conns)

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """一个写事务。异常时回滚并把异常抛出去。"""
        with self._write_lock:
            conn = self.connection
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    # ------------------------------------------------------------- 迁移 ---

    def migrate(self) -> int:
        """建表并把版本推进到 :data:`SCHEMA_VERSION`。返回最终版本号。幂等。"""
        conn = self.connection
        with self.write() as wconn:
            wconn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = int(row["v"]) if row and row["v"] is not None else 0
        for index in range(current, SCHEMA_VERSION):
            with self.write() as wconn:
                for statement in _MIGRATIONS[index]:
                    wconn.execute(statement)
                wconn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (index + 1,)
                )
        return SCHEMA_VERSION

    def close(self) -> None:
        """关掉**全部**已登记的连接。

        曾经的实现是「关掉调用线程那一条+ 推进 generation 让别的线程下次自毁」，
        因为 ``check_same_thread=True`` 会以 ``ProgrammingError`` 拒绝跨线程
        ``close()`` —— 而它正好是 ``sqlite3.Error`` 的子类，一旦被 ``suppress``
        吞掉，连接会一条都没关上而日志毫无异常（这个坑真的踩过一次）。

        现在 :meth:`_new_connection` 用 ``check_same_thread=False``，跨线程 close
        合法，于是可以在这里一次关干净。**这不是「放宽了安全」**：一条连接仍然只
        被一个线程使用（``threading.local`` 保证），``check_same_thread`` 防的
        misuse 我们本来就没做。

        必须真的关干净的原因：Python 3.14 的 ``sqlite3`` 会为每个未关闭的连接发
        ``ResourceWarning``，而本项目 ``filterwarnings = error`` —— 那些「留给
        解释器退出回收」的连接会直接变成测试失败。
        """
        mine: sqlite3.Connection | None = getattr(self._local, "conn", None)
        with self._conns_lock:
            self._generation += 1
            conns, self._all_conns = list(self._all_conns), []
        for conn in conns:
            if conn is mine:
                continue
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        if mine is not None:
            with contextlib.suppress(sqlite3.Error):
                mine.close()
        self._local.conn = None
        self._local.generation = self._generation

    # --------------------------------------------------------------- KV ---

    def kv_get(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def kv_set(self, key: str, value: str) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
