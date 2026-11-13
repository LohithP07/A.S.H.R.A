from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from ashra.db import Database, SQLiteSessionStore


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "data" / "ashra.db")
        self.db.initialize()

    def test_schema_is_idempotent(self) -> None:
        self.db.initialize()
        self.db.initialize()
        self.assertEqual(self.db.count_sessions(), 0)

    def test_session_round_trip(self) -> None:
        session_id = self.db.ensure_session()
        self.db.append_exchange(session_id, "hello", "good evening")

        history = self.db.history(session_id)
        self.assertEqual(
            history,
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "good evening"},
            ],
        )
        self.assertEqual(self.db.count_sessions(), 1)
        self.assertEqual(self.db.count_messages(), 2)

    def test_existing_session_id_is_reused(self) -> None:
        self.assertEqual(
            self.db.ensure_session("session-12345678"),
            "session-12345678",
        )
        self.assertEqual(
            self.db.ensure_session("session-12345678"),
            "session-12345678",
        )

    def test_invalid_session_id_gets_a_new_one(self) -> None:
        session_id = self.db.ensure_session("short")
        self.assertNotEqual(session_id, "short")
        self.assertGreaterEqual(len(session_id), 8)

    def test_history_is_trimmed_to_the_limit(self) -> None:
        session_id = self.db.ensure_session()
        for index in range(3):
            self.db.append_exchange(
                session_id,
                f"question {index}",
                f"answer {index}",
                max_messages=4,
            )

        history = self.db.history(session_id, limit=4)
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["content"], "question 1")
        self.assertEqual(history[-1]["content"], "answer 2")

    def test_pruning_removes_stale_sessions_and_messages(self) -> None:
        session_id = self.db.ensure_session()
        self.db.append_exchange(session_id, "hello", "hi")

        with self.db._connect() as connection:
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (time.time() - 10_000, session_id),
            )

        removed = self.db.prune_sessions(ttl_seconds=60)
        self.assertEqual(removed, 1)
        self.assertEqual(self.db.count_sessions(), 0)
        self.assertEqual(self.db.count_messages(), 0)

    def test_audit_round_trip(self) -> None:
        event_id = self.db.record_audit(
            "chat",
            "ask",
            status="ok",
            request_id="req-1",
            session_id="session-12345678",
            detail={"nodes": [1, 2]},
        )
        self.assertGreater(event_id, 0)

        events = self.db.list_audit()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "chat")
        self.assertEqual(events[0]["detail"], {"nodes": [1, 2]})

    def test_audit_can_be_filtered(self) -> None:
        self.db.record_audit("chat", "ask")
        self.db.record_audit("tool", "fs_read")

        events = self.db.list_audit(event="tool")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "fs_read")

    def test_sqlite_session_store_matches_the_protocol(self) -> None:
        store = SQLiteSessionStore(self.db, max_messages=4)
        session_id = store.ensure()
        store.append_exchange(session_id, "hello", "hi")

        self.assertEqual(
            store.history(session_id),
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )
        store.max_messages = 2
        self.assertEqual(len(store.history(session_id)), 2)


if __name__ == "__main__":
    unittest.main()
