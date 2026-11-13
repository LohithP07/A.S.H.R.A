from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ashra.api import create_app
from ashra.chat import ChatService
from ashra.db import Database
from ashra.errors import ApprovalRequired
from ashra.memory import MemoryStore
from ashra.notes import Note
from ashra.sessions import SessionStore
from ashra.tools import Permission, build_default_registry


def make_note(
    note_id: int = 1,
    title: str = "Pricing Strategy",
    text: str = "Our pricing strategy is value based.",
    *,
    path: str = "pricing.md",
) -> Note:
    return Note(
        id=note_id,
        label=title,
        title=title,
        group="Strategy",
        path=path,
        excerpt=text,
        text=text,
        words=len(text.split()),
    )


class FakeProvider:
    def __init__(self, answer: str = "Grounded answer.") -> None:
        self.answer = answer
        self.system = ""
        self.calls = 0

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        self.system = system
        return self.answer


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "data" / "test.db")
        self.store = MemoryStore(self.db)

    def test_add_and_search_a_confirmed_memory(self) -> None:
        memory = self.store.add(
            "The user prefers dark roast coffee.",
            kind="preference",
            status="confirmed",
        )

        results = self.store.search("coffee preference")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], memory["id"])
        self.assertEqual(results[0]["kind"], "preference")
        self.assertGreater(results[0]["score"], 0)
        self.assertIn("dark roast", results[0]["content"])

    def test_proposals_are_not_retrieved_until_confirmed(self) -> None:
        proposal = self.store.propose("The user likes oat milk.")

        self.assertEqual(self.store.search("oat milk"), [])
        self.assertEqual(self.store.stats()["proposed"], 1)

        confirmed = self.store.confirm(proposal["id"])

        self.assertIsNotNone(confirmed)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(len(self.store.search("oat milk")), 1)

    def test_duplicate_memory_is_reinforced_not_repeated(self) -> None:
        first = self.store.add("We ship on Fridays.", status="confirmed")
        second = self.store.add("we ship on fridays", status="confirmed")

        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["reinforcement"], 2)
        self.assertEqual(len(self.store.list(status="confirmed")), 1)

    def test_propose_from_text_splits_atomic_claims(self) -> None:
        proposals = self.store.propose_from_text(
            "I prefer dark roast coffee. We ship on Fridays.",
            source="chat",
        )

        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(item["status"] == "proposed" for item in proposals))
        self.assertEqual(
            {item["kind"] for item in proposals},
            {"preference", "fact"},
        )

    def test_remember_explicit_confirms_immediately(self) -> None:
        memories = self.store.remember_explicit(
            "Remember that our pricing is value based.",
            source="capture",
        )

        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["status"], "confirmed")
        self.assertGreaterEqual(memories[0]["confidence"], 0.9)

    def test_ingest_notes_is_idempotent(self) -> None:
        note = make_note()

        first = self.store.ingest_notes([note])
        second = self.store.ingest_notes([note])

        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(self.store.stats()["total"], 1)
        self.assertEqual(self.store.list(status="confirmed")[0]["kind"], "document")

    def test_graph_links_memories_that_share_an_entity(self) -> None:
        first = self.store.add(
            "Project Atlas uses Postgres.",
            status="confirmed",
        )
        second = self.store.add(
            "Project Atlas ships on Fridays.",
            status="confirmed",
        )

        graph = self.store.graph()

        ids = {node["id"] for node in graph["nodes"]}
        self.assertEqual(ids, {first["id"], second["id"]})
        self.assertEqual(len(graph["links"]), 1)
        self.assertIn(graph["links"][0]["kind"], {"shared_entity", "duplicate"})

    def test_update_and_forget(self) -> None:
        memory = self.store.add("The user likes tea.", status="confirmed")

        updated = self.store.update(memory["id"], content="The user likes coffee.")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["content"], "The user likes coffee.")

        self.assertTrue(self.store.forget(memory["id"]))
        self.assertIsNone(self.store.get(memory["id"]))
        self.assertFalse(self.store.forget(memory["id"]))

    def test_secrets_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add("api_key = sk-abcdefghijklmnopqrstuvwxyz", status="confirmed")

    def test_stats_report_lifecycle_and_health(self) -> None:
        self.store.add("We ship on Fridays.", status="confirmed")
        self.store.propose("The user likes oat milk.")

        stats = self.store.stats()

        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["confirmed"], 1)
        self.assertEqual(stats["proposed"], 1)
        self.assertIn("health", stats)
        self.assertIn("orphaned", stats["health"])

    def test_consolidate_is_safe_on_a_clean_store(self) -> None:
        self.store.add("We ship on Fridays.", status="confirmed")
        result = self.store.consolidate()
        self.assertEqual(result["archived"], 0)
        self.assertEqual(result["merged_groups"], 0)

    def test_memory_tools_respect_permissions(self) -> None:
        self.store.add("The user prefers dark roast coffee.", status="confirmed")
        registry = build_default_registry(Path(self.tmp.name), memory=self.store)

        result = registry.invoke(
            "memory_search",
            {"query": "coffee"},
            granted=Permission.READ,
        )
        self.assertEqual(len(result["memories"]), 1)

        with self.assertRaises(ApprovalRequired):
            registry.invoke(
                "memory_remember",
                {"content": "The user likes tea."},
                granted=Permission.ACT,
            )


class MemoryChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "config.json").write_text(
            json.dumps(
                {
                    "api_key": "test-key",
                    "model": "test-model",
                    "provider": "anthropic",
                }
            ),
            encoding="utf-8",
        )
        self.db = Database(self.base / "data" / "test.db")
        self.store = MemoryStore(self.db)
        self.provider = FakeProvider()

    def service(self) -> ChatService:
        return ChatService(
            self.base,
            sessions=SessionStore(),
            notes_loader=lambda: [],
            provider_factory=lambda _config: self.provider,
            memory=self.store,
        )

    def test_memory_can_answer_when_no_note_matches(self) -> None:
        self.store.add(
            "The user prefers dark roast coffee.",
            kind="preference",
            status="confirmed",
        )

        result = self.service().ask("What coffee do I prefer?")

        self.assertEqual(result.answer, "Grounded answer.")
        self.assertEqual(self.provider.calls, 1)
        self.assertIn("MEMORY", self.provider.system)
        self.assertIn("dark roast", self.provider.system)
        self.assertEqual(result.sources[0]["kind"], "memory")


class MemoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "notes").mkdir()
        self.db = Database(self.base / "data" / "test.db")
        self.app = create_app(self.base, database=self.db)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_create_list_and_search(self) -> None:
        response = self.client.post(
            "/memory",
            json={
                "content": "The user prefers dark roast coffee.",
                "kind": "preference",
                "status": "confirmed",
            },
        )
        self.assertEqual(response.status_code, 200)
        memory_id = response.json()["memory"]["id"]

        listing = self.client.get("/memory").json()
        self.assertEqual(listing["memories"][0]["id"], memory_id)
        self.assertEqual(listing["stats"]["confirmed"], 1)

        search = self.client.get("/memory/search", params={"q": "coffee"}).json()
        self.assertEqual(search["memories"][0]["id"], memory_id)

    def test_proposal_lifecycle(self) -> None:
        proposal = self.client.post(
            "/memory",
            json={"content": "The user likes oat milk.", "kind": "preference"},
        ).json()["memory"]
        self.assertEqual(proposal["status"], "proposed")

        proposals = self.client.get("/memory/proposals").json()
        self.assertEqual(proposals["memories"][0]["id"], proposal["id"])

        confirmed = self.client.post(
            f"/memory/{proposal['id']}/confirm"
        ).json()["memory"]
        self.assertEqual(confirmed["status"], "confirmed")

        rejected = self.client.post(
            f"/memory/{proposal['id']}/reject"
        ).json()["memory"]
        self.assertEqual(rejected["status"], "rejected")

    def test_propose_from_text(self) -> None:
        response = self.client.post(
            "/memory/propose",
            json={"text": "I prefer dark roast coffee. We ship on Fridays."},
        )
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        self.assertTrue(all(item["status"] == "proposed" for item in payload["memories"]))

    def test_graph_and_timeline(self) -> None:
        for content in (
            "Project Atlas uses Postgres.",
            "Project Atlas ships on Fridays.",
        ):
            self.client.post(
                "/memory",
                json={"content": content, "status": "confirmed"},
            )

        graph = self.client.get("/memory/graph").json()
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertEqual(len(graph["links"]), 1)

        timeline = self.client.get("/memory/timeline").json()
        self.assertEqual(len(timeline["memories"]), 2)

    def test_update_and_forget_require_confirmation(self) -> None:
        memory_id = self.client.post(
            "/memory",
            json={"content": "The user likes tea.", "status": "confirmed"},
        ).json()["memory"]["id"]

        updated = self.client.patch(
            f"/memory/{memory_id}",
            json={"content": "The user likes coffee.", "pinned": True},
        ).json()["memory"]
        self.assertEqual(updated["content"], "The user likes coffee.")
        self.assertTrue(updated["pinned"])

        refused = self.client.post(f"/memory/{memory_id}/forget", json={})
        self.assertEqual(refused.status_code, 400)

        removed = self.client.post(
            f"/memory/{memory_id}/forget",
            json={"confirm": True},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertIsNone(self.client.get(f"/memory/{memory_id}").json().get("memory"))


if __name__ == "__main__":
    unittest.main()
