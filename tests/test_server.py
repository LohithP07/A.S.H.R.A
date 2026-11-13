from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ashra.api import MAX_BODY_BYTES, create_app
from ashra.chat import ChatResult, ChatService
from ashra.db import Database, SQLiteSessionStore
from ashra.errors import NotConfiguredError
from ashra.notes import Note


class FakeChat:
    def __init__(self, result: ChatResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def ask(self, question: str, session_id: str | None = None) -> ChatResult:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def stream(self, question: str, session_id: str | None = None):
        if self.error is not None:
            raise self.error
        assert self.result is not None
        result = self.result
        yield {"event": "start", "session_id": result.session_id, "model": result.model}
        yield {"event": "sources", "sources": result.sources, "nodes": result.nodes}
        for word in result.answer.split():
            yield {"event": "token", "text": f"{word} "}
        yield {
            "event": "done",
            "answer": result.answer,
            "nodes": result.nodes,
            "sources": result.sources,
            "session_id": result.session_id,
            "model": result.model,
        }


def result(answer: str = "Test answer.") -> ChatResult:
    return ChatResult(
        answer=answer,
        nodes=[0],
        sources=[{"id": 0, "label": "Test Note"}],
        session_id="session-12345678",
        model="test-model",
    )


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "viewer").mkdir()
        (self.base / "viewer" / "index.html").write_text(
            "<h1>A.S.H.R.A</h1>",
            encoding="utf-8",
        )
        (self.base / "notes").mkdir()
        (self.base / "notes" / "pricing.md").write_text(
            "# Pricing\n\nValue based pricing.\n",
            encoding="utf-8",
        )
        self.db = Database(self.base / "data" / "test.db")
        self.chat = FakeChat(result())
        self.app = create_app(self.base, chat=self.chat, database=self.db)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    # ------------------------------------------------------------------
    # viewer and health
    # ------------------------------------------------------------------
    def test_viewer_is_served_with_branding(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("A.S.H.R.A", response.text)

    def test_config_path_is_blocked(self) -> None:
        self.assertEqual(self.client.get("/config.json").status_code, 404)
        self.assertEqual(self.client.get("/.env").status_code, 404)

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["name"], "A.S.H.R.A")

    def test_status_reports_the_vault_and_tools(self) -> None:
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["notes"], 1)
        self.assertEqual(payload["tools"], 9)
        self.assertFalse(payload["configured"])
        self.assertNotIn("api_key", payload)

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------
    def test_malformed_json_is_rejected(self) -> None:
        response = self.client.post(
            "/chat",
            content=b"{",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_chat_returns_the_service_result(self) -> None:
        response = self.client.post("/chat", json={"question": "hello"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["answer"], "Test answer.")
        self.assertEqual(payload["nodes"], [0])
        self.assertEqual(payload["session_id"], "session-12345678")

    def test_chat_maps_not_configured_to_503(self) -> None:
        self.chat.error = NotConfiguredError("No key.")
        response = self.client.post("/chat", json={"question": "hello"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "model_not_configured")

    def test_payload_limit_is_enforced(self) -> None:
        response = self.client.post(
            "/chat",
            content=b"x" * (MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertIn("error", response.json())

    def test_chat_is_audited(self) -> None:
        self.client.post("/chat", json={"question": "hello"})
        events = self.client.get("/audit").json()["events"]
        self.assertTrue(any(event["event"] == "chat" for event in events))
        self.assertTrue(any(event["status"] == "ok" for event in events))

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------
    def test_stream_returns_events(self) -> None:
        response = self.client.post("/chat/stream", json={"question": "hello"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertIn("event: start", response.text)
        self.assertIn("event: sources", response.text)
        self.assertIn("event: token", response.text)
        self.assertIn("event: done", response.text)
        self.assertIn("Test answer.", response.text)

    def test_stream_maps_not_configured(self) -> None:
        self.chat.error = NotConfiguredError("No key.")
        response = self.client.post("/chat/stream", json={"question": "hello"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: error", response.text)
        self.assertIn("model_not_configured", response.text)

    # ------------------------------------------------------------------
    # sessions, tools, audit
    # ------------------------------------------------------------------
    def test_session_history_is_served(self) -> None:
        self.db.append_exchange("session-12345678", "hello", "hi")
        response = self.client.get("/sessions/session-12345678")
        self.assertEqual(response.status_code, 200)
        messages = response.json()["messages"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "assistant")

    def test_tools_list_hides_write_tools_from_read_callers(self) -> None:
        response = self.client.get("/tools")
        self.assertEqual(response.status_code, 200)
        names = [tool["name"] for tool in response.json()["tools"]]
        self.assertIn("fs_read", names)
        self.assertNotIn("vault_remember", names)

        response = self.client.get("/tools?permission=act")
        names = [tool["name"] for tool in response.json()["tools"]]
        self.assertIn("vault_remember", names)

    def test_tool_invoke_reads_a_note(self) -> None:
        response = self.client.post(
            "/tools/invoke",
            json={"name": "fs_read", "arguments": {"relative_path": "pricing.md"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Value based pricing", response.json()["result"]["content"])

    def test_tool_invoke_blocks_path_traversal(self) -> None:
        response = self.client.post(
            "/tools/invoke",
            json={"name": "fs_read", "arguments": {"relative_path": "../config.json"}},
        )
        self.assertEqual(response.status_code, 400)

    def test_tool_invoke_enforces_permission_and_approval(self) -> None:
        response = self.client.post(
            "/tools/invoke",
            json={"name": "vault_remember", "arguments": {"text": "hello there"}},
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            "/tools/invoke",
            json={
                "name": "vault_remember",
                "arguments": {"text": "hello there"},
                "permission": "act",
            },
        )
        self.assertEqual(response.status_code, 409)

    def test_tool_invoke_rejects_bad_arguments(self) -> None:
        response = self.client.post(
            "/tools/invoke",
            json={"name": "fs_read", "arguments": {}},
        )
        self.assertEqual(response.status_code, 400)

    def test_audit_can_be_filtered(self) -> None:
        self.client.post(
            "/tools/invoke",
            json={"name": "fs_read", "arguments": {"relative_path": "pricing.md"}},
        )
        response = self.client.get("/audit?event=tool")
        self.assertEqual(response.status_code, 200)
        events = response.json()["events"]
        self.assertTrue(events)
        self.assertTrue(all(event["event"] == "tool" for event in events))


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[dict[str, str]] = []

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        self.messages = messages
        return "Grounded answer."


def parse_sse(text: str) -> list[dict]:
    events: list[dict] = []
    for block in text.strip().split("\n\n"):
        data = [
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


class ApiIntegrationTests(unittest.TestCase):
    """Exercise the real ChatService, SQLite store, and SSE path together."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "viewer").mkdir()
        (self.base / "viewer" / "index.html").write_text(
            "<h1>A.S.H.R.A</h1>",
            encoding="utf-8",
        )
        (self.base / "notes").mkdir()
        (self.base / "notes" / "pricing.md").write_text(
            "# Pricing\n\nOur pricing strategy is value based.\n",
            encoding="utf-8",
        )
        (self.base / "config.json").write_text(
            json.dumps(
                {
                    "api_key": "test-key",
                    "model": "test-model",
                    "provider": "anthropic",
                    "max_notes": 3,
                }
            ),
            encoding="utf-8",
        )
        self.db = Database(self.base / "data" / "test.db")
        self.provider = CountingProvider()
        note = Note(
            id=0,
            label="Pricing Strategy",
            title="Pricing Strategy",
            group="Strategy",
            path="pricing.md",
            excerpt="Our pricing strategy is value based.",
            text="Our pricing strategy is value based.",
            words=6,
        )
        self.chat = ChatService(
            self.base,
            sessions=SQLiteSessionStore(self.db),
            notes_loader=lambda: [note],
            provider_factory=lambda _config: self.provider,
        )
        self.app = create_app(self.base, chat=self.chat, database=self.db)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_chat_persists_the_session(self) -> None:
        first = self.client.post(
            "/chat",
            json={"question": "What is our pricing strategy?"},
        )
        self.assertEqual(first.status_code, 200)
        session_id = first.json()["session_id"]
        self.assertTrue(session_id)

        second = self.client.post(
            "/chat",
            json={
                "question": "And what about wholesale?",
                "session_id": session_id,
            },
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["session_id"], session_id)
        self.assertEqual(self.provider.calls, 2)

        history = self.client.get(f"/sessions/{session_id}").json()["messages"]
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0]["content"], "What is our pricing strategy?")
        self.assertEqual(history[-1]["role"], "assistant")

    def test_stream_persists_the_session_and_audits_it(self) -> None:
        response = self.client.post(
            "/chat/stream",
            json={"question": "What is our pricing strategy?"},
        )
        self.assertEqual(response.status_code, 200)

        events = parse_sse(response.text)
        start = next(event for event in events if event["event"] == "start")
        done = next(event for event in events if event["event"] == "done")
        self.assertEqual(done["answer"], "Grounded answer.")
        self.assertEqual(start["session_id"], done["session_id"])

        history = self.client.get(
            f"/sessions/{done['session_id']}"
        ).json()["messages"]
        self.assertEqual(len(history), 2)

        audit = self.client.get("/audit?event=chat").json()["events"]
        self.assertTrue(
            any(
                event["action"] == "stream"
                and event["session_id"] == done["session_id"]
                for event in audit
            )
        )


if __name__ == "__main__":
    unittest.main()
