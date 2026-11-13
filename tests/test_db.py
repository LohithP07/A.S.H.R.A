from __future__ import annotations

import sqlite3
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

    # ------------------------------------------------------------------
    # connectors and sources (schema v4)
    # ------------------------------------------------------------------
    def test_schema_reports_version_four(self) -> None:
        connection = sqlite3.connect(str(self.db.path))
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, 4)

    def test_source_upsert_is_idempotent_by_external_id(self) -> None:
        source_id, created = self.db.upsert_source(
            connector_id="gmail",
            kind="email",
            external_id="uid-1",
            subject="First subject",
        )
        self.assertTrue(created)
        self.assertTrue(source_id.startswith("src_"))

        same_id, created_again = self.db.upsert_source(
            connector_id="gmail",
            kind="email",
            external_id="uid-1",
            subject="Edited subject",
        )
        self.assertFalse(created_again)
        self.assertEqual(same_id, source_id)
        self.assertEqual(self.db.count_sources("gmail"), 1)
        self.assertEqual(self.db.get_source(source_id)["subject"], "Edited subject")

    def test_sources_without_an_external_id_are_not_merged(self) -> None:
        first, _ = self.db.upsert_source(connector_id="files", kind="file")
        second, _ = self.db.upsert_source(connector_id="files", kind="file")
        self.assertNotEqual(first, second)
        self.assertEqual(self.db.count_sources("files"), 2)

    def test_equal_external_ids_stay_separate_across_connectors(self) -> None:
        self.db.upsert_source(connector_id="gmail", kind="email", external_id="1")
        self.db.upsert_source(connector_id="gcal", kind="event", external_id="1")
        self.assertEqual(self.db.count_sources(), 2)

    def test_connector_state_round_trip(self) -> None:
        self.db.upsert_connector_state(
            "gmail",
            kind="gmail",
            credential_ref="ashra/gmail",
            scopes={"folders": ["INBOX"]},
            capabilities={"read": True, "write": False},
            status="ready",
            last_sync_at=123.0,
        )
        again = self.db.upsert_connector_state("gmail", kind="gmail", status="error", error="boom")

        self.assertEqual(again["credential_ref"], "ashra/gmail")
        self.assertEqual(again["scopes"], {"folders": ["INBOX"]})
        self.assertEqual(again["status"], "error")
        self.assertEqual(again["error"], "boom")
        # A later error does not erase the last successful sync.
        self.assertEqual(again["last_sync_at"], 123.0)
        self.assertEqual(len(self.db.list_connector_states()), 1)

    def test_legacy_sensitivity_values_migrate_forward(self) -> None:
        path = Path(self.tmp.name) / "legacy" / "ashra.db"
        Database(path).initialize()

        connection = sqlite3.connect(str(path))
        try:
            connection.execute(
                "INSERT INTO memories (id, created_at, updated_at, last_seen_at, "
                "kind, content, normalized, sensitivity, status) "
                "VALUES ('m1', 0, 0, 0, 'fact', 'legacy row', 'legacy row', "
                "'normal', 'confirmed')"
            )
            connection.execute(
                "INSERT INTO memories (id, created_at, updated_at, last_seen_at, "
                "kind, content, normalized, sensitivity, status) "
                "VALUES ('m2', 0, 0, 0, 'fact', 'private row', 'private row', "
                "'private', 'confirmed')"
            )
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        finally:
            connection.close()

        Database(path).initialize()

        connection = sqlite3.connect(str(path))
        try:
            rows = dict(connection.execute("SELECT id, sensitivity FROM memories"))
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(rows, {"m1": "internal", "m2": "private"})
        self.assertEqual(version, 4)


if __name__ == "__main__":
    unittest.main()
