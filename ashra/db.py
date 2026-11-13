"""SQLite persistence for sessions, messages, and the audit log.

The database is deliberately small and local.  It is the durable counterpart
to ``ashra.sessions.SessionStore``: the in-memory store stays useful for tests
and one-off scripts, while the API injects ``SQLiteSessionStore`` so a browser
reload no longer loses the conversation.

Every connection is opened per operation.  SQLite's WAL mode plus a busy
timeout keeps concurrent request handlers from stepping on each other without
holding a process-wide lock during model calls.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .sessions import SESSION_RE

SCHEMA_VERSION = 2
DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_APPROVAL_TTL_SECONDS = 15 * 60
DEFAULT_MAX_MESSAGES = 8


class Database:
    """A tiny SQLite wrapper with explicit, short-lived connections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._initialized = False

    # ------------------------------------------------------------------
    # connection / schema
    # ------------------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the data directory and schema. Safe to call repeatedly."""
        with self._lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_messages_session
                        ON messages(session_id, id);

                    CREATE TABLE IF NOT EXISTS audit_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at REAL NOT NULL,
                        event TEXT NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'ok',
                        actor TEXT NOT NULL DEFAULT 'local',
                        permission TEXT,
                        request_id TEXT,
                        session_id TEXT,
                        detail TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_audit_created
                        ON audit_events(created_at DESC);

                    CREATE TABLE IF NOT EXISTS approvals (
                        id TEXT PRIMARY KEY,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        session_id TEXT,
                        tool TEXT NOT NULL,
                        arguments TEXT NOT NULL,
                        permission TEXT,
                        request_id TEXT,
                        question TEXT,
                        state TEXT,
                        result TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_approvals_status
                        ON approvals(status, created_at DESC);
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._initialized = True

    # ------------------------------------------------------------------
    # sessions and messages
    # ------------------------------------------------------------------
    def ensure_session(self, session_id: str | None = None) -> str:
        self.initialize()
        now = time.time()
        with self._lock, self._connect() as connection:
            if session_id and SESSION_RE.match(session_id):
                connection.execute(
                    """
                    INSERT INTO sessions (id, created_at, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
                    """,
                    (session_id, now, now),
                )
                return session_id

            new_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
                (new_id, now, now),
            )
            return new_id

    def history(self, session_id: str, limit: int | None = None) -> list[dict[str, str]]:
        self.initialize()
        with self._lock, self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT role, content FROM messages "
                    "WHERE session_id = ? ORDER BY id",
                    (session_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT role, content FROM messages "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, max(1, int(limit))),
                ).fetchall()
                rows = list(reversed(rows))
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def append_exchange(
        self,
        session_id: str,
        user: str,
        assistant: str,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        self.initialize()
        now = time.time()
        keep = max(2, int(max_messages))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at",
                (session_id, now, now),
            )
            connection.executemany(
                "INSERT INTO messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (session_id, "user", user, now),
                    (session_id, "assistant", assistant, now),
                ],
            )
            connection.execute(
                """
                DELETE FROM messages
                WHERE session_id = ?
                  AND id NOT IN (
                      SELECT id FROM messages
                      WHERE session_id = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (session_id, session_id, keep),
            )

    def prune_sessions(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
        self.initialize()
        cutoff = time.time() - max(60, int(ttl_seconds))
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE updated_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount or 0)

    def count_sessions(self) -> int:
        self.initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
        return int(row["n"] if row else 0)

    def count_messages(self) -> int:
        self.initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        return int(row["n"] if row else 0)

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------
    def record_audit(
        self,
        event: str,
        action: str,
        *,
        status: str = "ok",
        actor: str = "local",
        permission: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        detail: Any = None,
    ) -> int:
        self.initialize()
        payload: str | None = None
        if detail is not None:
            try:
                payload = json.dumps(detail, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                payload = json.dumps({"detail": str(detail)}, ensure_ascii=False)
            if len(payload) > 4000:
                payload = payload[:4000] + "…"

        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_events
                    (created_at, event, action, status, actor,
                     permission, request_id, session_id, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    str(event),
                    str(action),
                    str(status),
                    str(actor),
                    permission,
                    request_id,
                    session_id,
                    payload,
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_audit(
        self,
        limit: int = 50,
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        limit = max(1, min(500, int(limit)))
        with self._lock, self._connect() as connection:
            if event:
                rows = connection.execute(
                    "SELECT * FROM audit_events WHERE event = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (event, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        events: list[dict[str, Any]] = []
        for row in rows:
            detail: Any = row["detail"]
            if detail:
                try:
                    detail = json.loads(detail)
                except (TypeError, ValueError):
                    pass
            events.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "event": row["event"],
                    "action": row["action"],
                    "status": row["status"],
                    "actor": row["actor"],
                    "permission": row["permission"],
                    "request_id": row["request_id"],
                    "session_id": row["session_id"],
                    "detail": detail,
                }
            )
        return events


    # ------------------------------------------------------------------
    # approvals
    # ------------------------------------------------------------------
    @staticmethod
    def _approval_dict(row: sqlite3.Row) -> dict[str, Any]:
        def decode(value: Any) -> Any:
            if not value:
                return None
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return value

        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "session_id": row["session_id"],
            "tool": row["tool"],
            "arguments": decode(row["arguments"]) or {},
            "permission": row["permission"],
            "request_id": row["request_id"],
            "question": row["question"],
            "state": decode(row["state"]),
            "result": decode(row["result"]),
        }

    def create_approval(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        permission: str | None = None,
        session_id: str | None = None,
        request_id: str | None = None,
        question: str | None = None,
        state: Any = None,
    ) -> str:
        self.initialize()
        approval_id = uuid.uuid4().hex
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals
                    (id, created_at, updated_at, status, session_id, tool,
                     arguments, permission, request_id, question, state)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    now,
                    now,
                    session_id,
                    str(tool),
                    json.dumps(arguments, ensure_ascii=False, default=str),
                    permission,
                    request_id,
                    question,
                    json.dumps(state, ensure_ascii=False, default=str) if state is not None else None,
                ),
            )
        return approval_id

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE id = ?",
                (str(approval_id),),
            ).fetchone()
        return self._approval_dict(row) if row else None

    def list_approvals(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.initialize()
        limit = max(1, min(200, int(limit)))
        with self._lock, self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM approvals WHERE status = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._approval_dict(row) for row in rows]

    def decide_approval(self, approval_id: str, approve: bool) -> dict[str, Any] | None:
        """Atomically move a pending approval to approved/denied.

        Returns the updated row, or ``None`` when the approval was already
        decided, expired, or never existed.
        """
        self.initialize()
        status = "approved" if approve else "denied"
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE approvals SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, time.time(), str(approval_id)),
            )
            if not cursor.rowcount:
                return None
        return self.get_approval(approval_id)

    def complete_approval(
        self,
        approval_id: str,
        status: str,
        result: Any = None,
    ) -> None:
        self.initialize()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE approvals SET status = ?, updated_at = ?, result = ? WHERE id = ?",
                (
                    str(status),
                    time.time(),
                    json.dumps(result, ensure_ascii=False, default=str)
                    if result is not None
                    else None,
                    str(approval_id),
                ),
            )

    def expire_approvals(
        self,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> int:
        self.initialize()
        cutoff = time.time() - max(60, int(ttl_seconds))
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE approvals SET status = 'expired', updated_at = ? "
                "WHERE status = 'pending' AND created_at < ?",
                (time.time(), cutoff),
            )
            return int(cursor.rowcount or 0)

    def count_approvals(self, status: str | None = "pending") -> int:
        self.initialize()
        with self._lock, self._connect() as connection:
            if status:
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM approvals WHERE status = ?",
                    (status,),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) AS n FROM approvals").fetchone()
        return int(row["n"] if row else 0)


class SQLiteSessionStore:
    """Session backend backed by :class:`Database`.

    It mirrors ``ashra.sessions.SessionStore`` so ``ChatService`` does not need
    to know whether history lives in memory or on disk.
    """

    def __init__(
        self,
        database: Database,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.database = database
        self.max_messages = max(2, int(max_messages))
        self.ttl_seconds = max(60, int(ttl_seconds))

    def ensure(self, session_id: str | None = None) -> str:
        self.database.prune_sessions(self.ttl_seconds)
        return self.database.ensure_session(session_id)

    def history(self, session_id: str) -> list[dict[str, str]]:
        return self.database.history(session_id, limit=self.max_messages)

    def append_exchange(self, session_id: str, user: str, assistant: str) -> None:
        self.database.append_exchange(
            session_id,
            user,
            assistant,
            max_messages=self.max_messages,
        )
