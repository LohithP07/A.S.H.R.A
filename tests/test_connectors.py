from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ashra.api import create_app
from ashra.connectors import (
    Candidate,
    Capabilities,
    Capability,
    Connector,
    ConnectorRegistry,
    NormalisedRecord,
    PollResult,
    Record,
    ReplayConnector,
    Source,
)
from ashra.credentials import MemoryVault
from ashra.db import Database
from ashra.errors import CapabilityDenied, ConnectorNotFound, RateLimited


class FixtureConnector(Connector):
    """A minimal read-only adapter with a declared mailbox scope."""

    def __init__(self, records=(), **kwargs):
        kwargs.setdefault("connector_id", "fixture")
        kwargs.setdefault("label", "Fixture mailbox")
        kwargs.setdefault("capabilities", {"read": True})
        kwargs.setdefault("scopes", {"folders": ["INBOX"]})
        kwargs.setdefault("credential_ref", "ashra/gmail")
        super().__init__(**kwargs)
        self.records = list(records)

    def poll(self, cursor=None):
        return PollResult(items=list(self.records), fetched=len(self.records))

    def normalise(self, record):
        return NormalisedRecord(
            source=Source(
                connector_id=self.id,
                kind=record.kind,
                external_id=record.external_id,
                thread_id=record.thread_id,
                subject=record.subject,
                occurred_at=record.occurred_at,
                sensitivity=record.sensitivity or "private",
            ),
            candidates=tuple(
                Candidate(**item) for item in record.payload.get("candidates", ())
            ),
        )


class BrokenConnector(Connector):
    def __init__(self, **kwargs):
        kwargs.setdefault("connector_id", "broken")
        kwargs.setdefault("capabilities", {"read": True})
        kwargs.setdefault("credential_ref", "ashra/calendar")
        super().__init__(**kwargs)

    def poll(self, cursor=None):
        raise RuntimeError("IMAP rejected the session")

    def normalise(self, record):
        return None


def email(external_id: str = "e1", **overrides) -> Record:
    payload = overrides.pop("payload", {})
    return Record(
        external_id=external_id,
        kind="email",
        subject="Re: wholesale pricing",
        occurred_at=1_760_000_000.0,
        thread_id="t1",
        payload=payload,
        **overrides,
    )


class CapabilityTests(unittest.TestCase):
    def test_read_only_is_the_default(self) -> None:
        caps = Capabilities()
        self.assertTrue(caps.allows(Capability.READ))
        self.assertFalse(caps.allows("draft"))
        self.assertFalse(caps.allows("write"))
        self.assertFalse(caps.allows("delete"))
        self.assertEqual(caps.as_list(), ["read"])

    def test_parses_names_lists_and_mappings(self) -> None:
        self.assertEqual(Capabilities.parse("read").as_dict()["read"], True)
        self.assertTrue(Capabilities.parse(["read", "draft"]).draft)
        self.assertTrue(Capabilities.parse({"read": True, "draft": True}).draft)
        self.assertFalse(Capabilities.parse({"read": True}).draft)

    def test_unknown_names_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Capabilities.parse(["read", "send"])
        with self.assertRaises(ValueError):
            Capabilities.parse({"read": True, "execute": True})

    def test_delete_requires_write(self) -> None:
        with self.assertRaises(ValueError):
            Capabilities(read=True, write=False, delete=True)

    def test_require_names_the_connector(self) -> None:
        with self.assertRaises(CapabilityDenied) as raised:
            Capabilities().require("write", "gmail")
        self.assertIn("gmail", str(raised.exception))
        self.assertIn("write", str(raised.exception))


class ConnectorTests(unittest.TestCase):
    def test_invalid_identity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FixtureConnector(connector_id="Gmail")
        with self.assertRaises(ValueError):
            FixtureConnector(credential_ref="not-a-ref")

    def test_guard_refuses_undeclared_capabilities(self) -> None:
        connector = FixtureConnector()
        connector.guard("read")
        with self.assertRaises(CapabilityDenied):
            connector.guard("write")

    def test_status_never_includes_a_secret(self) -> None:
        vault = MemoryVault({"ashra/gmail": "top-secret-app-password"})
        connector = FixtureConnector()
        status = connector.status()
        self.assertEqual(status["credential_ref"], "ashra/gmail")
        self.assertNotIn("top-secret-app-password", str(status))
        self.assertNotIn("top-secret-app-password", str(vault))

    def test_rate_limit_is_enforced_per_connector(self) -> None:
        now = [100.0]
        connector = FixtureConnector(min_interval_seconds=60, clock=lambda: now[0])
        registry = ConnectorRegistry([connector])

        registry.ingest("fixture")
        with self.assertRaises(RateLimited):
            registry.ingest("fixture")
        now[0] += 61.0
        registry.ingest("fixture")

    def test_force_bypasses_the_rate_limit(self) -> None:
        clock = [100.0]
        connector = FixtureConnector(min_interval_seconds=600, clock=lambda: clock[0])
        registry = ConnectorRegistry([connector])
        registry.ingest("fixture")
        registry.ingest("fixture", force=True)

    def test_failures_are_recorded_as_health(self) -> None:
        connector = BrokenConnector()
        registry = ConnectorRegistry([connector])
        with self.assertRaises(RuntimeError):
            registry.ingest("broken")

        health = connector.health()
        self.assertEqual(health.status, "error")
        self.assertFalse(health.ok)
        self.assertIn("IMAP", health.last_error or "")


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "ashra.db")
        self.db.initialize()

    def test_duplicate_ids_are_rejected(self) -> None:
        registry = ConnectorRegistry([FixtureConnector()])
        with self.assertRaises(ValueError):
            registry.register(FixtureConnector())

    def test_unknown_ids_raise(self) -> None:
        registry = ConnectorRegistry()
        with self.assertRaises(ConnectorNotFound):
            registry.get("gmail")
        self.assertIsNone(registry.find("gmail"))

    def test_require_checks_the_capability(self) -> None:
        registry = ConnectorRegistry([FixtureConnector()])
        registry.require("fixture", "read")
        with self.assertRaises(CapabilityDenied):
            registry.require("fixture", "write")

    def test_ingest_persists_idempotent_sources(self) -> None:
        record = email(
            payload={
                "candidates": [
                    {
                        "kind": "task",
                        "title": "Answer the wholesale question",
                        "person": "Sarah",
                        "confidence": 0.7,
                    }
                ]
            }
        )
        registry = ConnectorRegistry([FixtureConnector([record])], database=self.db)

        first = registry.ingest("fixture")
        self.assertEqual(first.created, 1)
        self.assertEqual(first.updated, 0)
        self.assertEqual(len(first.candidates), 1)
        self.assertEqual(self.db.count_sources("fixture"), 1)

        second = registry.ingest("fixture", force=True)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.updated, 1)
        self.assertEqual(self.db.count_sources("fixture"), 1)

        source = self.db.list_sources("fixture")[0]
        self.assertEqual(source["subject"], "Re: wholesale pricing")
        self.assertEqual(first.candidates[0]["source_ref"], source["id"])

    def test_candidates_inherit_the_stronger_sensitivity(self) -> None:
        record = email(
            sensitivity="private",
            payload={
                "candidates": [
                    {"kind": "memory", "title": "Pricing note", "sensitivity": "public"}
                ]
            },
        )
        registry = ConnectorRegistry([FixtureConnector([record])], database=self.db)
        result = registry.ingest("fixture")
        self.assertEqual(result.candidates[0]["sensitivity"], "private")

    def test_connector_state_is_persisted_without_secrets(self) -> None:
        registry = ConnectorRegistry([FixtureConnector([email()])], database=self.db)
        registry.ingest("fixture")

        state = self.db.get_connector_state("fixture")
        assert state is not None
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["capabilities"]["read"], True)
        self.assertEqual(state["capabilities"]["write"], False)
        self.assertEqual(state["credential_ref"], "ashra/gmail")
        self.assertIsNotNone(state["last_sync_at"])

    def test_one_broken_connector_does_not_block_another(self) -> None:
        registry = ConnectorRegistry(
            [BrokenConnector(), FixtureConnector([email()])],
            database=self.db,
        )
        with self.assertRaises(RuntimeError):
            registry.ingest("broken")
        result = registry.ingest("fixture")
        self.assertEqual(result.created, 1)

        self.assertEqual(registry.get("broken").health().status, "error")
        self.assertEqual(registry.get("fixture").health().status, "ready")

    def test_bad_records_are_skipped_not_fatal(self) -> None:
        bad = Record(external_id="bad", kind="email", payload={"candidates": [{"title": "x"}]})
        registry = ConnectorRegistry([FixtureConnector([bad, email()])], database=self.db)
        result = registry.ingest("fixture")
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.created, 1)

    def test_credential_readiness_reports_names_only(self) -> None:
        vault = MemoryVault({"ashra/gmail": "imap-secret"})
        registry = ConnectorRegistry([FixtureConnector()])
        rows = registry.credentials(vault)
        self.assertEqual(rows[0]["credential_ref"], "ashra/gmail")
        self.assertTrue(rows[0]["ready"])
        self.assertNotIn("imap-secret", str(rows))

        empty = registry.credentials(MemoryVault())
        self.assertFalse(empty[0]["ready"])


class ReplayConnectorTests(unittest.TestCase):
    def test_pages_through_records_and_reports_exhaustion(self) -> None:
        connector = ReplayConnector(
            [
                {"external_id": "1", "kind": "note", "subject": "one"},
                {"external_id": "2", "kind": "note", "subject": "two"},
                {"external_id": "3", "kind": "note", "subject": "three"},
            ],
            page_size=2,
        )
        first = connector.run()
        self.assertEqual(first.fetched, 2)
        self.assertEqual(first.cursor, "2")
        self.assertFalse(first.exhausted)

        second = connector.run(first.cursor)
        self.assertEqual(second.fetched, 1)
        self.assertIsNone(second.cursor)
        self.assertTrue(second.exhausted)

    def test_maps_candidates_and_classifies_ordinary_text(self) -> None:
        connector = ReplayConnector(
            [
                {
                    "external_id": "1",
                    "kind": "message",
                    "subject": "Dentist appointment",
                    "payload": {
                        "body": "medical appointment on Tuesday",
                        "candidates": [{"kind": "task", "title": "Book dentist"}],
                    },
                }
            ]
        )
        result = connector.run()
        normalised = connector.normalise(result.items[0])
        assert normalised is not None
        self.assertEqual(normalised.source.sensitivity, "sensitive")
        self.assertEqual(normalised.candidates[0].title, "Book dentist")

    def test_rejects_non_record_input(self) -> None:
        with self.assertRaises(TypeError):
            ReplayConnector([object()])


class ConnectorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "viewer").mkdir()
        (self.base / "viewer" / "index.html").write_text("<h1>A.S.H.R.A</h1>", encoding="utf-8")
        self.db = Database(self.base / "data" / "test.db")
        self.vault = MemoryVault({"ashra/gmail": "top-secret-app-password"})
        self.registry = ConnectorRegistry([FixtureConnector([email()])], database=self.db)
        self.app = create_app(
            self.base,
            database=self.db,
            connectors=self.registry,
            vault=self.vault,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_lists_declared_connectors_without_secrets(self) -> None:
        response = self.client.get("/connectors")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["connectors"][0]["id"], "fixture")
        self.assertEqual(payload["connectors"][0]["capabilities"]["read"], True)
        self.assertTrue(payload["credentials"][0]["ready"])
        self.assertEqual(payload["vault"]["name"], "memory")
        self.assertNotIn("top-secret-app-password", response.text)

    def test_get_one_connector(self) -> None:
        response = self.client.get("/connectors/fixture")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["connector"]["label"], "Fixture mailbox")

    def test_unknown_connector_is_404(self) -> None:
        self.assertEqual(self.client.get("/connectors/gmail").status_code, 404)

    def test_status_counts_connectors(self) -> None:
        payload = self.client.get("/status").json()
        self.assertEqual(payload["connectors"], 1)


if __name__ == "__main__":
    unittest.main()
