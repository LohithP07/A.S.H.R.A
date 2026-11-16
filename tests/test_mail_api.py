from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ashra.api import create_app
from ashra.connectors import ConnectorRegistry, GmailConnector
from ashra.crypto import PlainCipher
from ashra.credentials import MemoryVault
from ashra.db import Database
from ashra.mail import MailService
from tests.mail_fakes import FakeImap, build_message

PASSWORD = "api-test-app-password"


def config_json(**overrides) -> str:
    gmail = {
        "enabled": True,
        "credential_ref": "ashra/gmail",
        "username": "owner@example.com",
        "mode": "read",
        "folders": ["INBOX", "[Gmail]/Sent Mail"],
        "retention_days": 90,
        "classify_with_model": False,
    }
    gmail.update(overrides)
    return json.dumps({"connectors": {"gmail": gmail}})


class MailApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "viewer").mkdir()
        (self.base / "viewer" / "index.html").write_text(
            "<h1>A.S.H.R.A</h1>", encoding="utf-8"
        )
        (self.base / "config.json").write_text(config_json(), encoding="utf-8")
        self.db = Database(self.base / "data" / "test.db")
        self.vault = MemoryVault({"ashra/gmail": PASSWORD})
        self.mailboxes = {
            "INBOX": [(1, build_message())],
            "[Gmail]/Sent Mail": [],
        }
        self.instances: list[FakeImap] = []
        self.mail = MailService(
            self.base,
            self.db,
            self.vault,
            cipher=PlainCipher(),
            imap_factory=self._factory,
        )
        self.client = self._client()

    # -- helpers -------------------------------------------------------
    def _factory(self, _host, _port, _timeout) -> FakeImap:
        client = FakeImap(self.mailboxes)
        self.instances.append(client)
        return client

    def _client(self) -> TestClient:
        registry = ConnectorRegistry(
            [
                GmailConnector(
                    username="owner@example.com",
                    secret_provider=lambda: PASSWORD,
                    folders=("INBOX", "[Gmail]/Sent Mail"),
                    min_interval_seconds=0,
                )
            ],
            database=self.db,
        )
        self.app = create_app(
            self.base,
            database=self.db,
            connectors=registry,
            vault=self.vault,
            mail=self.mail,
        )
        client = TestClient(self.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def _sync(self) -> dict:
        response = self.client.post("/connectors/gmail/sync", json={"force": True})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # -- sync ----------------------------------------------------------
    def test_sync_caches_mail_and_reports_proposals(self) -> None:
        payload = self._sync()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["fetched"], 1)
        self.assertEqual(payload["cached"], 1)
        self.assertEqual(payload["proposals_created"], 1)
        self.assertNotIn(PASSWORD, str(payload))

    def test_unconfigured_connector_is_409_with_the_fix(self) -> None:
        (self.base / "config.json").write_text(config_json(enabled=False), encoding="utf-8")
        response = self.client.post("/connectors/gmail/sync", json={"force": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "mail_not_configured")
        self.assertIn("connectors.gmail", response.json()["detail"])

    def test_missing_credential_is_409_and_names_the_reference(self) -> None:
        mail = MailService(
            self.base,
            self.db,
            MemoryVault(),
            cipher=PlainCipher(),
            imap_factory=self._factory,
        )
        self.mail = mail
        self.client = self._client()

        response = self.client.post("/connectors/gmail/sync", json={"force": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "credential_missing")
        self.assertIn("ashra/gmail", response.json()["detail"])

    def test_a_broken_mailbox_is_502_not_a_crash(self) -> None:
        def broken(_host, _port, _timeout):
            raise OSError("connection refused")

        self.mail = MailService(
            self.base,
            self.db,
            self.vault,
            cipher=PlainCipher(),
            imap_factory=broken,
        )
        self.client = self._client()

        response = self.client.post("/connectors/gmail/sync", json={"force": True})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "connector_error")
        self.assertNotIn(PASSWORD, response.text)

    def test_another_connector_cannot_use_the_mail_sync_path(self) -> None:
        response = self.client.post("/connectors/gcal/sync", json={"force": True})
        self.assertEqual(response.status_code, 404)

    def test_a_failed_sync_is_audited_without_the_secret(self) -> None:
        self.client.post("/connectors/gcal/sync", json={"force": True})
        self.mailbox_failure = None
        (self.base / "config.json").write_text(config_json(enabled=False), encoding="utf-8")
        response = self.client.post("/connectors/gmail/sync", json={"force": True})
        self.assertEqual(response.status_code, 409)

        events = self.db.list_audit(event="mail", limit=10)
        self.assertTrue(events)
        self.assertEqual(events[0]["status"], "error")
        self.assertNotIn(PASSWORD, json.dumps(events))

    # -- reading -------------------------------------------------------
    def test_threads_and_one_decrypted_message(self) -> None:
        self._sync()

        threads = self.client.get("/mail/threads")
        self.assertEqual(threads.status_code, 200)
        self.assertEqual(len(threads.json()["threads"]), 1)
        self.assertEqual(threads.json()["mailbox"]["connector"], "gmail")

        message_id = self.db.list_mail_messages()[0]["id"]
        response = self.client.get(f"/mail/messages/{message_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()["message"]["body"]
        self.assertIn("send the wholesale price list", body)
        self.assertIn("<untrusted-content", response.json()["message"]["envelope"])

    def test_an_unknown_message_is_404(self) -> None:
        response = self.client.get("/mail/messages/msg_does_not_exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "not_found")

    def test_a_body_from_another_cipher_is_409(self) -> None:
        from ashra.crypto import DpapiCipher

        self._sync()
        message_id = self.db.list_mail_messages()[0]["id"]

        self.mail = MailService(
            self.base,
            self.db,
            self.vault,
            cipher=DpapiCipher(),
            imap_factory=self._factory,
        )
        self.client = self._client()
        response = self.client.get(f"/mail/messages/{message_id}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "cache_not_readable")

    def test_connector_sources_are_listed(self) -> None:
        self._sync()
        response = self.client.get("/connectors/gmail/sources")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["sources"]), 1)

    # -- proposals -----------------------------------------------------
    def test_proposals_can_be_listed_confirmed_and_rejected(self) -> None:
        self._sync()
        listing = self.client.get("/proposals")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["counts"]["proposed"], 1)
        proposal_id = listing.json()["proposals"][0]["id"]

        confirmed = self.client.post(f"/proposals/{proposal_id}/confirm")
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["proposal"]["status"], "confirmed")
        self.assertEqual(
            self.client.get("/proposals").json()["counts"]["proposed"], 0
        )

        rejected = self.client.post(f"/proposals/{proposal_id}/reject")
        self.assertEqual(rejected.status_code, 200)
        # A decided proposal is terminal, not flipped.
        self.assertEqual(rejected.json()["proposal"]["status"], "confirmed")

    def test_a_proposal_can_be_rejected_directly(self) -> None:
        self._sync()
        proposal_id = self.client.get("/proposals").json()["proposals"][0]["id"]
        response = self.client.post(f"/proposals/{proposal_id}/reject")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["proposal"]["status"], "rejected")

    def test_an_unknown_proposal_is_404(self) -> None:
        response = self.client.post("/proposals/prop_missing/confirm")
        self.assertEqual(response.status_code, 404)

    def test_decisions_are_audited(self) -> None:
        self._sync()
        proposal_id = self.client.get("/proposals").json()["proposals"][0]["id"]
        self.client.post(f"/proposals/{proposal_id}/confirm")
        events = self.db.list_audit(event="mail", limit=5)
        self.assertIn("proposal_confirm", [event["action"] for event in events])

    # -- home and status -----------------------------------------------
    def test_home_surfaces_proposals_in_needs_you(self) -> None:
        self._sync()
        payload = self.client.get("/home").json()
        self.assertEqual(len(payload["needs_you"]["proposals"]), 1)
        self.assertEqual(payload["needs_you"]["count"], 1)
        self.assertIn("mail proposal", payload["brief"]["detail"])
        self.assertEqual(len(payload["recent"]["mail"]), 1)

    def test_status_reports_the_mailbox_without_a_secret(self) -> None:
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        mail = response.json()["mail"]
        self.assertEqual(mail["connector"], "gmail")
        self.assertTrue(mail["enabled"])
        self.assertTrue(mail["credential_ready"])
        self.assertEqual(mail["mode"], "read")
        self.assertIn("proposals", response.json())
        self.assertNotIn(PASSWORD, response.text)

    # -- retention -----------------------------------------------------
    def test_purge_endpoint_empties_the_cache(self) -> None:
        self._sync()
        self.assertEqual(self.db.mail_stats()["messages"], 1)
        response = self.client.post("/cache/purge")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["purged"], 0)  # nothing is old yet
        self.assertEqual(response.json()["stats"]["messages"], 1)

        self.db.purge_mail()
        response = self.client.post("/cache/purge")
        self.assertEqual(response.json()["stats"]["messages"], 0)


if __name__ == "__main__":
    unittest.main()
