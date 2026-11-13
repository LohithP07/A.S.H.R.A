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

SCHEMA_VERSION = 4
DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_APPROVAL_TTL_SECONDS = 15 * 60
DEFAULT_MAX_MESSAGES = 8


class Database:
    """A tiny SQLite wrapper with explicit, short-lived connections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._initialized = False
        self.fts_enabled = False

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

                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL,
                        kind TEXT NOT NULL,
                        content TEXT NOT NULL,
                        normalized TEXT NOT NULL,
                        entities TEXT NOT NULL DEFAULT '[]',
                        source TEXT NOT NULL DEFAULT 'manual',
                        source_ref TEXT,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        sensitivity TEXT NOT NULL DEFAULT 'internal',
                        status TEXT NOT NULL DEFAULT 'proposed',
                        pinned INTEGER NOT NULL DEFAULT 0,
                        reinforcement INTEGER NOT NULL DEFAULT 1,
                        metadata TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_memories_status
                        ON memories(status, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_memories_kind
                        ON memories(kind, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_memories_source_ref
                        ON memories(source_ref);

                    CREATE TABLE IF NOT EXISTS memory_links (
                        source TEXT NOT NULL,
                        target TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'shared_entity',
                        weight REAL NOT NULL DEFAULT 1.0,
                        PRIMARY KEY (source, target, kind)
                    );

                    CREATE INDEX IF NOT EXISTS idx_memory_links_source
                        ON memory_links(source);
                    CREATE INDEX IF NOT EXISTS idx_memory_links_target
                        ON memory_links(target);

                    CREATE TABLE IF NOT EXISTS memory_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_id TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        event TEXT NOT NULL,
                        detail TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_memory_events_memory
                        ON memory_events(memory_id, id);

                    CREATE TABLE IF NOT EXISTS connectors (
                        id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        credential_ref TEXT,
                        scopes TEXT NOT NULL DEFAULT '{}',
                        capabilities TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'unknown',
                        error TEXT,
                        last_sync_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS sources (
                        id TEXT PRIMARY KEY,
                        connector_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        external_id TEXT,
                        thread_id TEXT,
                        subject TEXT,
                        occurred_at REAL,
                        local_path TEXT,
                        sensitivity TEXT NOT NULL DEFAULT 'private',
                        metadata TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_identity
                        ON sources(connector_id, kind, external_id)
                        WHERE external_id IS NOT NULL;

                    CREATE INDEX IF NOT EXISTS idx_sources_connector
                        ON sources(connector_id, occurred_at DESC);
                    """
                )
                # FTS5 is optional: some Python builds ship SQLite without it.
                # The memory store falls back to a full scan when unavailable.
                try:
                    connection.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                        "USING fts5(content, memory_id UNINDEXED)"
                    )
                    self.fts_enabled = True
                except sqlite3.OperationalError:
                    self.fts_enabled = False
                # Forward-only data migrations.  Schema v4 replaced the old
                # three-value sensitivity vocabulary with the PRD's five
                # classes; existing rows move forward and never back.
                current = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if current < 4:
                    connection.execute(
                        "UPDATE memories SET sensitivity = 'internal' "
                        "WHERE sensitivity = 'normal'"
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

    def recent_sessions(self, limit: int = 8) -> list[dict[str, Any]]:
        """Return recent conversations with a preview of the last exchange."""
        self.initialize()
        limit = max(1, min(50, int(limit)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.id,
                    s.created_at,
                    s.updated_at,
                    (
                        SELECT content FROM messages m
                        WHERE m.session_id = s.id AND m.role = 'user'
                        ORDER BY m.id DESC LIMIT 1
                    ) AS last_question,
                    (
                        SELECT content FROM messages m
                        WHERE m.session_id = s.id AND m.role = 'assistant'
                        ORDER BY m.id DESC LIMIT 1
                    ) AS last_answer,
                    (
                        SELECT COUNT(*) FROM messages m
                        WHERE m.session_id = s.id
                    ) AS message_count
                FROM sessions s
                WHERE EXISTS (
                    SELECT 1 FROM messages m WHERE m.session_id = s.id
                )
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "last_question": row["last_question"] or "",
                "last_answer": row["last_answer"] or "",
                "messages": int(row["message_count"] or 0),
            }
            for row in rows
        ]

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

    # ------------------------------------------------------------------
    # connectors and sources
    # ------------------------------------------------------------------
    @staticmethod
    def _connector_dict(row: sqlite3.Row) -> dict[str, Any]:
        def decode(value: Any, fallback: Any) -> Any:
            if not value:
                return fallback
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return fallback

        return {
            "id": row["id"],
            "kind": row["kind"],
            "credential_ref": row["credential_ref"],
            "scopes": decode(row["scopes"], {}),
            "capabilities": decode(row["capabilities"], {}),
            "status": row["status"],
            "error": row["error"],
            "last_sync_at": row["last_sync_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def upsert_connector_state(
        self,
        connector_id: str,
        *,
        kind: str,
        credential_ref: str | None = None,
        scopes: Any = None,
        capabilities: Any = None,
        status: str = "unknown",
        error: str | None = None,
        last_sync_at: float | None = None,
    ) -> dict[str, Any]:
        """Record a connector's declaration and last-known health.

        Only configuration and status are stored.  Credential *references* may
        appear; credential values must never reach this table.
        """
        self.initialize()
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO connectors
                    (id, kind, credential_ref, scopes, capabilities, status,
                     error, last_sync_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    credential_ref = COALESCE(
                        excluded.credential_ref, connectors.credential_ref
                    ),
                    scopes = CASE
                        WHEN excluded.scopes = '{}' THEN connectors.scopes
                        ELSE excluded.scopes
                    END,
                    capabilities = CASE
                        WHEN excluded.capabilities = '{}' THEN connectors.capabilities
                        ELSE excluded.capabilities
                    END,
                    status = excluded.status,
                    error = excluded.error,
                    last_sync_at = COALESCE(
                        excluded.last_sync_at, connectors.last_sync_at
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    str(connector_id),
                    str(kind),
                    credential_ref,
                    json.dumps(scopes or {}, ensure_ascii=False, default=str),
                    json.dumps(capabilities or {}, ensure_ascii=False, default=str),
                    str(status),
                    error,
                    last_sync_at,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM connectors WHERE id = ?",
                (str(connector_id),),
            ).fetchone()
        return self._connector_dict(row) if row else {}

    def get_connector_state(self, connector_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM connectors WHERE id = ?",
                (str(connector_id),),
            ).fetchone()
        return self._connector_dict(row) if row else None

    def list_connector_states(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM connectors ORDER BY id"
            ).fetchall()
        return [self._connector_dict(row) for row in rows]

    @staticmethod
    def _source_dict(row: sqlite3.Row) -> dict[str, Any]:
        metadata: Any = row["metadata"]
        if metadata:
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                pass
        return {
            "id": row["id"],
            "connector_id": row["connector_id"],
            "kind": row["kind"],
            "external_id": row["external_id"],
            "thread_id": row["thread_id"],
            "subject": row["subject"],
            "occurred_at": row["occurred_at"],
            "local_path": row["local_path"],
            "sensitivity": row["sensitivity"],
            "metadata": metadata,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def upsert_source(
        self,
        *,
        connector_id: str,
        kind: str,
        external_id: str | None = None,
        thread_id: str | None = None,
        subject: str | None = None,
        occurred_at: float | None = None,
        local_path: str | None = None,
        sensitivity: str = "private",
        metadata: Any = None,
    ) -> tuple[str, bool]:
        """Insert or update one external record.

        Returns ``(source_id, created)``.  Identity is
        ``(connector_id, kind, external_id)`` so a repeated poll updates the
        existing row instead of duplicating it.  A record without an external id
        cannot be deduplicated and is always inserted.
        """
        self.initialize()
        now = time.time()
        payload = json.dumps(metadata, ensure_ascii=False, default=str) if metadata else None
        with self._lock, self._connect() as connection:
            existing = None
            if external_id:
                existing = connection.execute(
                    "SELECT id FROM sources "
                    "WHERE connector_id = ? AND kind = ? AND external_id = ?",
                    (str(connector_id), str(kind), str(external_id)),
                ).fetchone()

            if existing is not None:
                source_id = str(existing["id"])
                connection.execute(
                    """
                    UPDATE sources SET
                        thread_id = COALESCE(?, thread_id),
                        subject = COALESCE(?, subject),
                        occurred_at = COALESCE(?, occurred_at),
                        local_path = COALESCE(?, local_path),
                        sensitivity = ?,
                        metadata = COALESCE(?, metadata),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        thread_id,
                        subject,
                        occurred_at,
                        local_path,
                        str(sensitivity),
                        payload,
                        now,
                        source_id,
                    ),
                )
                return source_id, False

            source_id = f"src_{uuid.uuid4().hex[:24]}"
            connection.execute(
                """
                INSERT INTO sources
                    (id, connector_id, kind, external_id, thread_id, subject,
                     occurred_at, local_path, sensitivity, metadata,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    str(connector_id),
                    str(kind),
                    external_id,
                    thread_id,
                    subject,
                    occurred_at,
                    local_path,
                    str(sensitivity),
                    payload,
                    now,
                    now,
                ),
            )
            return source_id, True

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE id = ?",
                (str(source_id),),
            ).fetchone()
        return self._source_dict(row) if row else None

    def list_sources(
        self,
        connector_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.initialize()
        limit = max(1, min(500, int(limit)))
        with self._lock, self._connect() as connection:
            if connector_id:
                rows = connection.execute(
                    "SELECT * FROM sources WHERE connector_id = ? "
                    "ORDER BY COALESCE(occurred_at, created_at) DESC LIMIT ?",
                    (str(connector_id), limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM sources "
                    "ORDER BY COALESCE(occurred_at, created_at) DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._source_dict(row) for row in rows]

    def count_sources(self, connector_id: str | None = None) -> int:
        self.initialize()
        with self._lock, self._connect() as connection:
            if connector_id:
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM sources WHERE connector_id = ?",
                    (str(connector_id),),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) AS n FROM sources").fetchone()
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
