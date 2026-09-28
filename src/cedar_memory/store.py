"""本机 SQLite 存储：每次操作独立连接，写入使用短事务。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import validation as v
from .models import MemoryRecord, MemoryStorageError, SessionSummary, StaleSummaryError

_APPLICATION_ID = 0x434D454D
_SCHEMA_VERSION = 1
_MAX_COUNTER = 2**63 - 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _scope(scope: str) -> str:
    return v.text(scope, "scope", 256, identifier=True)


def _key(key: str) -> str:
    return v.text(key, "key", 256, identifier=True)


class MemoryStore:
    """同步存储接口。请在应用启动时显式构造；import 不读写文件。

    不支持 :memory: 或网络共享文件。异步应用使用 CedarMemory 门面，
    或将此类调用放入工作线程，避免 SQLite 锁等待阻塞事件循环。
    """

    def __init__(self, path: str | Path, *, timeout: float = 5.0):
        if str(path) == ":memory:":
            raise ValueError("需要持久化数据库文件路径")
        self.path = Path(path).resolve()
        self.timeout = v.timeout(timeout)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise MemoryStorageError("无法创建记忆数据目录") from None
        with self._connection(write=True) as conn:
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id == 0 and version == 0 and not tables:
                conn.execute(
                    "CREATE TABLE scopes (scope TEXT PRIMARY KEY, revision INTEGER NOT NULL)"
                )
                conn.execute("""CREATE TABLE memories (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, key TEXT NOT NULL,
                    value TEXT NOT NULL, source_request_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL, UNIQUE(scope, key))""")
                conn.execute("""CREATE TABLE summaries (
                    scope TEXT NOT NULL, session_id TEXT NOT NULL, text TEXT NOT NULL,
                    covered_through INTEGER NOT NULL, revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(scope, session_id))""")
                conn.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            elif app_id != _APPLICATION_ID or version != _SCHEMA_VERSION:
                raise MemoryStorageError("不是支持的 Cedar Memory 数据库版本")
        # WAL 只能在事务外切换；所有连接仍需遵守单写者约束。
        with self._connection() as conn:
            conn.commit()
            conn.execute("PRAGMA journal_mode = WAL")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except sqlite3.Error:
            if conn is not None:
                conn.rollback()
            raise MemoryStorageError("记忆数据库操作失败；请检查可用性与锁竞争") from None
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _revision(conn: sqlite3.Connection, scope: str) -> int:
        row = conn.execute("SELECT revision FROM scopes WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _invalidate(conn: sqlite3.Connection, scope: str) -> None:
        conn.execute(
            """INSERT INTO scopes VALUES (?, 1)
            ON CONFLICT(scope) DO UPDATE SET revision=revision+1""",
            (scope,),
        )
        # 无摘要依赖图时采取保守策略，清除该用户范围的全部摘要。
        conn.execute("DELETE FROM summaries WHERE scope=?", (scope,))

    def revision(self, scope: str) -> int:
        _scope(scope)
        with self._connection() as conn:
            return self._revision(conn, scope)

    def put(self, scope: str, key: str, value: str, *, source_request_id: str) -> MemoryRecord:
        _scope(scope)
        _key(key)
        v.text(value, "value", 4096)
        v.text(source_request_id, "source_request_id", 256, identifier=True)
        with self._connection(write=True) as conn:
            conn.execute(
                """INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value,
                source_request_id=excluded.source_request_id, updated_at=excluded.updated_at""",
                (str(uuid4()), scope, key, value, source_request_id, _now()),
            )
            self._invalidate(conn, scope)
            row = conn.execute(
                "SELECT * FROM memories WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
            return MemoryRecord(**dict(row))

    def get(self, scope: str, key: str) -> MemoryRecord | None:
        _scope(scope)
        _key(key)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
            return MemoryRecord(**dict(row)) if row else None

    def list(self, scope: str, *, limit: int = 100) -> tuple[MemoryRecord, ...]:
        _scope(scope)
        v.integer(limit, "limit", 1, 1000)
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE scope=? ORDER BY key LIMIT ?",
                (scope, limit),
            )
            return tuple(MemoryRecord(**dict(row)) for row in rows)

    def delete(self, scope: str, key: str) -> bool:
        _scope(scope)
        _key(key)
        with self._connection(write=True) as conn:
            count = conn.execute(
                "DELETE FROM memories WHERE scope=? AND key=?", (scope, key)
            ).rowcount
            # 即使键已不存在，也使在途摘要失效，遵守用户的忘记请求。
            self._invalidate(conn, scope)
            return bool(count)

    def save_summary(
        self,
        scope: str,
        session_id: str,
        text: str,
        *,
        covered_through: int,
        expected_revision: int,
    ) -> SessionSummary:
        _scope(scope)
        v.text(session_id, "session_id", 256, identifier=True)
        v.text(text, "summary", 8192)
        v.integer(covered_through, "covered_through", 1, _MAX_COUNTER)
        v.integer(expected_revision, "expected_revision", 0, _MAX_COUNTER)
        with self._connection(write=True) as conn:
            if self._revision(conn, scope) != expected_revision:
                raise StaleSummaryError("记忆已变化，拒绝保存过期摘要")
            previous = conn.execute(
                "SELECT covered_through FROM summaries WHERE scope=? AND session_id=?",
                (scope, session_id),
            ).fetchone()
            if previous and previous[0] >= covered_through:
                raise StaleSummaryError("已存在相同或更新轮次的摘要")
            summary = SessionSummary(
                scope, session_id, text, covered_through, expected_revision, _now()
            )
            conn.execute(
                """INSERT INTO summaries VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, session_id) DO UPDATE SET text=excluded.text,
                covered_through=excluded.covered_through, revision=excluded.revision,
                updated_at=excluded.updated_at""",
                (
                    scope,
                    session_id,
                    text,
                    covered_through,
                    expected_revision,
                    summary.updated_at,
                ),
            )
            return summary

    def get_summary(self, scope: str, session_id: str) -> SessionSummary | None:
        _scope(scope)
        v.text(session_id, "session_id", 256, identifier=True)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM summaries WHERE scope=? AND session_id=? AND revision=?",
                (scope, session_id, self._revision(conn, scope)),
            ).fetchone()
            return SessionSummary(**dict(row)) if row else None

    def clear_summary(self, scope: str, session_id: str) -> bool:
        _scope(scope)
        v.text(session_id, "session_id", 256, identifier=True)
        with self._connection(write=True) as conn:
            count = conn.execute(
                "SELECT count(*) FROM summaries WHERE scope=? AND session_id=?",
                (scope, session_id),
            ).fetchone()[0]
            # 首版只有 scope 版本，因此清理时也保守使同 scope 摘要全部失效。
            self._invalidate(conn, scope)
            return bool(count)

    def snapshot(
        self,
        scope: str,
        *,
        keys: tuple[str, ...] = (),
        query: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
    ) -> tuple[tuple[MemoryRecord, ...], SessionSummary | None, int, bool]:
        """同一读事务内获取偏好与摘要。keys 和 query 同时提供时取交集。

        query 是字面子串匹配，仅对 ASCII 忽略大小写，不是分词或语义搜索。
        未指定 keys/query 时不读取偏好，避免默认把全部个人信息送给模型。
        """
        _scope(scope)
        if not isinstance(keys, tuple) or len(keys) > 100:
            raise ValueError("keys 必须是最多 100 项的 tuple")
        for key in keys:
            _key(key)
        if query is not None:
            v.text(query, "query", 256)
        if session_id is not None:
            v.text(session_id, "session_id", 256, identifier=True)
        v.integer(limit, "limit", 1, 1000)
        with self._connection() as conn:
            revision = self._revision(conn, scope)
            records = ()
            if keys or query is not None:
                conditions = ["scope=?"]
                args: list[object] = [scope]
                if keys:
                    conditions.append("key IN (" + ",".join("?" for _ in keys) + ")")
                    args.extend(keys)
                if query is not None:
                    conditions.append(
                        "(instr(lower(key), lower(?))>0 OR instr(lower(value), lower(?))>0)"
                    )
                    args.extend((query, query))
                args.append(limit + 1)
                rows = conn.execute(
                    "SELECT * FROM memories WHERE "
                    + " AND ".join(conditions)
                    + " ORDER BY key LIMIT ?",
                    args,
                )
                records = tuple(MemoryRecord(**dict(row)) for row in rows)
            summary = None
            if session_id is not None:
                row = conn.execute(
                    "SELECT * FROM summaries WHERE scope=? AND session_id=? AND revision=?",
                    (scope, session_id, revision),
                ).fetchone()
                summary = SessionSummary(**dict(row)) if row else None
            return records[:limit], summary, revision, len(records) > limit
