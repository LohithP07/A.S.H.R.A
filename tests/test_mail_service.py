from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ashra.crypto import DpapiCipher, PlainCipher
from ashra.db import Database
from ashra.credentials import MemoryVault
from ashra.errors import ConfigError, ConnectorError, CredentialError
from ashra.mail import MAX_MODEL_CALLS, MailService, merge_classification
from ashra.mailparse import IGNORE, INFORMATIONAL, REPLY_NEEDED, Classification
from tests.mail_fakes import FakeImap, build_message

PASSWORD = "app-password-do-not-store"


def config_json(**overrides) -> str:
    gmail = {
        "enabled": True,
        "credential_ref": "ashra/gmail",
        "username": "owner@example.com",
        "mode": "read",
        "folders": ["INBOX", "[Gmail]/Sent Mail"],
        "retention_days": 90,
        "max_per_poll": 50,
        "classify_with_model": False,
    }
    gmail.update(overrides)
    return json.dumps({"connectors": {"gmail": gmail}})


class MailServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.base.joinpath("config.json").write_text(config_json(), encoding="utf-8")
        self.db = Database(self.base / "data" / "ashra.db")
        self.vault = MemoryVault({"ashra/gmail": PASSWORD})
        self.instances: list[FakeImap] = []
        self.mailboxes = {
            folder: [] for folder in ("INBOX", "[Gmail]/Sent Mail")
        }

    # -- helpers -------------------------------------------------------
    def service(self, **kwargs) -> MailService:
        kwargs.setdefault("cipher", PlainCipher())
        kwargs.setdefault("imap_factory", self._factory)
        return MailService(self.base, self.db, self.vault, **kwargs)

    def _factory(self, _host, _port, _timeout) -> FakeImap:
        client = FakeImap(self.mailboxes)
        self.instances.append(client)
        return client

    def write_config(self, text: str) -> None:
        self.base.joinpath("config.json").write_text(text, encoding="utf-8")

    # -- the happy path ------------------------------------------------
    def test_sync_caches_an_encrypted_body_and_a_proposal(self) -> None:
        self.mailboxes["INBOX"].append(
            (1, build_message(subject="Wholesale pricing"))
        )
        service = self.service()
        result = service.sync(force=True)

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["cached"], 1)
        self.assertEqual(result["proposals_created"], 1)
        self.assertTrue(result["exhausted"])

        stats = self.db.mail_stats()
        self.assertEqual(stats["messages"], 1)
        self.assertEqual(stats["threads"], 1)

        rows = self.db.list_mail_messages(connector_id="gmail")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Wholesale pricing")
        self.assertEqual(rows[0]["from_addr"], "sarah@example.com")
        self.assertEqual(rows[0]["folder"], "INBOX")
        # Only a hash and a length are visible; the body is ciphertext.
        self.assertEqual(len(rows[0]["body_hash"]), 64)
        self.assertGreater(rows[0]["body_chars"], 0)

        stored = self.db.get_mail_message(rows[0]["id"])
        self.assertIsInstance(stored["body_cipher"], bytes)
        # The blob carries a scheme marker, so an unencrypted cache is visible
        # rather than silent.  (Encryption itself is proven in CryptoTests.)
        self.assertTrue(stored["body_cipher"].startswith(b"plain1\x00"))

        proposals = service.proposals()
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["origin"], "gmail")
        self.assertEqual(proposals[0]["status"], "proposed")
        self.assertIn("send", proposals[0]["title"].lower())
        self.assertIsNotNone(proposals[0]["due_at"])

    def test_the_cached_body_can_be_read_back_locally(self) -> None:
        self.mailboxes["INBOX"].append((1, build_message()))
        service = self.service()
        service.sync(force=True)
        message_id = self.db.list_mail_messages()[0]["id"]

        payload = service.message(message_id)
        self.assertIn("send the wholesale price list", payload["message"]["body"])
        self.assertIn("<untrusted-content", payload["message"]["envelope"])
        self.assertTrue(payload["message"]["envelope"].rstrip().endswith(
            "</untrusted-content>"
        ))
        self.assertEqual(payload["message"]["injection"]["risk"], 0.0)
        self.assertIsNotNone(payload["message"]["classification"])

    def test_an_unknown_message_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.service().message("msg_missing")

    def test_a_thread_groups_a_reply_with_its_parent(self) -> None:
        self.mailboxes["INBOX"].append(
            (1, build_message(message_id="<a@x>", subject="Deck"))
        )
        self.mailboxes["INBOX"].append(
            (
                2,
                build_message(
                    message_id="<b@x>",
                    subject="Re: Deck",
                    references=("<a@x>",),
                    body="Thanks. Could you confirm the total by Friday?\n",
                ),
            )
        )
        service = self.service()
        service.sync(force=True)

        threads = service.threads()
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["messages"], 2)
        self.assertEqual(threads[0]["thread_id"], "a@x")

    # -- incremental sync ---------------------------------------------
    def test_a_second_sync_reads_only_new_mail(self) -> None:
        self.mailboxes["INBOX"].append((1, build_message(message_id="<a@x>")))
        service = self.service()
        service.sync(force=True)

        second = service.sync(force=True)
        self.assertEqual(second["fetched"], 0)
        self.assertEqual(second["cached"], 0)
        self.assertEqual(self.db.mail_stats()["messages"], 1)

        self.mailboxes["INBOX"].append((2, build_message(message_id="<b@x>")))
        third = service.sync(force=True)
        self.assertEqual(third["fetched"], 1)
        self.assertEqual(self.db.mail_stats()["messages"], 2)

    def test_re_reading_a_message_never_duplicates_it(self) -> None:
        self.mailboxes["INBOX"].append((1, build_message(message_id="<a@x>")))
        service = self.service()
        service.sync(force=True)
        first = service.proposals()

        # Rewind the cursor: the same message is polled again.
        self.db.set_cursor("gmail", "INBOX", uidvalidity=7, uid=0)
        second = service.sync(force=True)

        self.assertEqual(second["cached"], 1)
        self.assertEqual(second["proposals_created"], 0)
        self.assertEqual(second["proposals_refreshed"], 1)
        self.assertEqual(self.db.mail_stats()["messages"], 1)
        self.assertEqual(len(service.proposals()), len(first))

    def test_a_decided_proposal_is_not_resurrected_by_a_sync(self) -> None:
        self.mailboxes["INBOX"].append((1, build_message()))
        service = self.service()
        service.sync(force=True)
        proposal = service.proposals()[0]
        service.decide(proposal["id"], status="rejected")

        self.db.set_cursor("gmail", "INBOX", uidvalidity=7, uid=0)
        service.sync(force=True)

        self.assertEqual(service.proposals(status="proposed"), [])
        decided = service.proposals(status="rejected")
        self.assertEqual(len(decided), 1)
        self.assertEqual(decided[0]["id"], proposal["id"])

    def test_a_read_only_folder_failure_is_a_config_error(self) -> None:
        self.mailboxes.pop("[Gmail]/Sent Mail", None)
        service = self.service()
        with self.assertRaises(Exception) as caught:
            service.sync(force=True)
        self.assertIn("[Gmail]/Sent Mail", str(caught.exception))
        # The failure is durable: /status can explain it after the connector
        # object is gone.
        self.assertIn("[Gmail]/Sent Mail", service.status()["last_error"])

    # -- permissions and configuration --------------------------------
    def test_a_disabled_connector_names_the_fix(self) -> None:
        self.write_config(config_json(enabled=False))
        with self.assertRaises(ConfigError) as caught:
            self.service().sync(force=True)
        self.assertIn("connectors.gmail", str(caught.exception))

    def test_a_missing_username_names_the_fix(self) -> None:
        self.write_config(config_json(username=""))
        with self.assertRaises(ConfigError) as caught:
            self.service().sync(force=True)
        self.assertIn("username", str(caught.exception))

    def test_a_missing_credential_names_the_fix(self) -> None:
        self.write_config(config_json())
        service = MailService(
            self.base, self.db, MemoryVault(), cipher=PlainCipher(), imap_factory=self._factory
        )
        with self.assertRaises(CredentialError) as caught:
            service.sync(force=True)
        self.assertIn("ashra/gmail", str(caught.exception))

    def test_write_mode_is_refused_by_configuration(self) -> None:
        self.write_config(config_json(mode="write"))
        with self.assertRaises(ConfigError) as caught:
            self.service().sync(force=True)
        self.assertIn("read-only", str(caught.exception))

    def test_status_explains_what_is_missing_without_a_secret(self) -> None:
        service = self.service()
        status = service.status()
        self.assertTrue(status["enabled"])
        self.assertTrue(status["configured"])
        self.assertTrue(status["credential_ready"])
        self.assertEqual(status["mode"], "read")
        self.assertEqual(status["folders"], ["INBOX", "[Gmail]/Sent Mail"])
        self.assertFalse(status["encrypted_at_rest"])  # PlainCipher is honest
        self.assertNotIn(PASSWORD, json.dumps(status))

    def test_status_reports_a_missing_credential(self) -> None:
        service = MailService(
            self.base, self.db, MemoryVault(), cipher=PlainCipher(), imap_factory=self._factory
        )
        status = service.status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["credential_ready"])
        self.assertTrue(any("vault" in item for item in status["limitations"]))

    # -- classification ------------------------------------------------
    def test_a_model_failure_is_a_visible_limitation_not_a_sync_failure(self) -> None:
        self.write_config(config_json(classify_with_model=True))
        # A question is reply_needed only at 0.60 confidence, so the model is
        # actually asked and its failure falls back to those rules.
        self.mailboxes["INBOX"].append(
            (1, build_message(body="Is the roastery open?\n"))
        )

        def broken(_config):
            raise RuntimeError("ollama refused the connection")

        service = self.service(provider_factory=broken)
        result = service.sync(force=True)
        self.assertEqual(result["cached"], 1)
        self.assertTrue(result["proposals_created"] >= 1)
        self.assertTrue(
            any("local model was unavailable" in item for item in result["limitations"])
        )

    def test_the_model_budget_is_bounded_per_sync(self) -> None:
        self.write_config(config_json(classify_with_model=True))
        self.mailboxes["INBOX"].extend(
            (
                index + 1,
                build_message(
                    message_id=f"<m{index}@x>",
                    body="The beans arrived this morning.\n",
                ),
            )
            for index in range(MAX_MODEL_CALLS + 5)
        )
        calls = {"n": 0}

        class Provider:
            def complete_json(self, _system, _messages):
                calls["n"] += 1
                return {"label": INFORMATIONAL, "confidence": 0.95}

        service = self.service(provider_factory=lambda _config: Provider())
        result = service.sync(force=True)

        self.assertEqual(calls["n"], MAX_MODEL_CALLS)
        self.assertEqual(result["classifier_calls"], MAX_MODEL_CALLS)
        self.assertTrue(
            any("Rules only after" in item for item in result["limitations"])
        )

    def test_a_confident_rule_answer_skips_the_model(self) -> None:
        self.write_config(config_json(classify_with_model=True))
        self.mailboxes["INBOX"].append(
            (1, build_message(subject="The weekly digest", body="News.", list_id="x"))
        )
        calls = {"n": 0}

        class Provider:
            def complete_json(self, _system, _messages):
                calls["n"] += 1
                return {"label": REPLY_NEEDED, "confidence": 0.99}

        service = self.service(provider_factory=lambda _config: Provider())
        service.sync(force=True)
        self.assertEqual(calls["n"], 0)

    def test_secret_content_never_reaches_the_model(self) -> None:
        self.write_config(config_json(classify_with_model=True))
        self.mailboxes["INBOX"].append(
            (
                1,
                build_message(
                    subject="Access",
                    body="Please confirm the password: hunter2 for the vault.\n",
                ),
            )
        )
        calls = {"n": 0}

        class Provider:
            def complete_json(self, _system, _messages):
                calls["n"] += 1
                return {"label": REPLY_NEEDED, "confidence": 0.99}

        service = self.service(provider_factory=lambda _config: Provider())
        service.sync(force=True)
        self.assertEqual(calls["n"], 0)
        self.assertEqual(service.proposals(), [])

    def test_a_cipher_failure_never_caches_a_plaintext_body(self) -> None:
        class BrokenCipher(PlainCipher):
            def protect(self, data: bytes) -> bytes:
                raise OSError("DPAPI is unavailable")

        self.mailboxes["INBOX"].append((1, build_message()))
        service = self.service(cipher=BrokenCipher())
        with self.assertRaises(ConnectorError) as caught:
            service.sync(force=True)
        self.assertIn("could not be encrypted", str(caught.exception))
        self.assertEqual(self.db.mail_stats()["messages"], 0)

    # -- retention -----------------------------------------------------
    def test_purge_removes_messages_outside_the_retention_window(self) -> None:
        self.write_config(config_json(retention_days=1))
        self.mailboxes["INBOX"].append((1, build_message()))
        service = self.service()
        service.sync(force=True)

        old = time.time() - 3 * 86_400
        connection = sqlite3.connect(self.db.path)
        try:
            connection.execute("UPDATE mail_messages SET cached_at = ?", (old,))
            connection.commit()
        finally:
            connection.close()

        result = service.purge()
        self.assertEqual(result["purged"], 1)
        self.assertEqual(result["stats"]["messages"], 0)
        self.assertEqual(self.db.list_mail_messages(), [])

    def test_purge_keeps_messages_inside_the_window(self) -> None:
        self.mailboxes["INBOX"].append((1, build_message()))
        service = self.service()
        service.sync(force=True)
        result = service.purge()
        self.assertEqual(result["purged"], 0)
        self.assertEqual(result["stats"]["messages"], 1)

    # -- secrets -------------------------------------------------------
    def test_the_password_is_not_written_to_the_database(self) -> None:
        self.mailboxes["INBOX"].append((1, build_message()))
        service = self.service()
        service.sync(force=True)

        raw = self.db.path.read_bytes()
        self.assertNotIn(PASSWORD.encode("utf-8"), raw)

        audit = self.db.list_audit(event="mail", limit=20)
        self.assertTrue(audit)
        self.assertNotIn(PASSWORD, json.dumps(audit))
        for event in audit:
            self.assertEqual(event["status"], "ok")

    def test_the_sync_audit_says_whether_the_cache_was_encrypted(self) -> None:
        self.mailboxes["INBOX"].append((1, build_message()))
        service = self.service()
        service.sync(force=True)
        event = self.db.list_audit(event="mail", limit=1)[0]
        self.assertEqual(event["action"], "sync")
        self.assertIn("encrypted", event["detail"])
        self.assertFalse(event["detail"]["encrypted"])


class MergeClassificationTests(unittest.TestCase):
    def test_a_more_confident_model_answer_wins(self) -> None:
        merged = merge_classification(
            Classification(INFORMATIONAL, 0.5, "rules"),
            Classification(REPLY_NEEDED, 0.9, "local model"),
        )
        self.assertEqual(merged.label, REPLY_NEEDED)

    def test_the_rules_win_when_they_are_more_confident(self) -> None:
        merged = merge_classification(
            Classification(REPLY_NEEDED, 0.75, "rules"),
            Classification(IGNORE, 0.4, "local model"),
        )
        self.assertEqual(merged.label, REPLY_NEEDED)

    def test_an_unknown_label_is_ignored(self) -> None:
        merged = merge_classification(
            Classification(INFORMATIONAL, 0.5, "rules"),
            Classification("suspend-the-rules", 0.99, "local model"),
        )
        self.assertEqual(merged.label, INFORMATIONAL)

    def test_a_missing_model_answer_changes_nothing(self) -> None:
        rules = Classification(INFORMATIONAL, 0.5, "rules")
        self.assertIs(merge_classification(rules, None), rules)


class EncryptedCacheTests(unittest.TestCase):
    """The cache really is ciphertext when the platform can encrypt."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.base.joinpath("config.json").write_text(config_json(), encoding="utf-8")
        self.db = Database(self.base / "data" / "ashra.db")
        self.vault = MemoryVault({"ashra/gmail": PASSWORD})
        self.mailboxes = {
            "INBOX": [(1, build_message())],
            "[Gmail]/Sent Mail": [],
        }

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is a Windows API")
    def test_dpapi_ciphertext_hides_the_body_and_still_decrypts(self) -> None:
        instances: list[FakeImap] = []

        def factory(_host, _port, _timeout):
            client = FakeImap(self.mailboxes)
            instances.append(client)
            return client

        service = MailService(
            self.base, self.db, self.vault, cipher=DpapiCipher(), imap_factory=factory
        )
        result = service.sync(force=True)
        self.assertTrue(result["cached"])

        connection = sqlite3.connect(self.db.path)
        try:
            blobs = [
                blob
                for (blob,) in connection.execute(
                    "SELECT body_cipher FROM mail_messages"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(len(blobs), 1)
        self.assertTrue(blobs[0].startswith(b"dpapi1\x00"))
        self.assertNotIn(b"wholesale price list", blobs[0])
        message_id = self.db.list_mail_messages()[0]["id"]
        payload = service.message(message_id)
        self.assertIn("wholesale price list", payload["message"]["body"])


if __name__ == "__main__":
    unittest.main()
