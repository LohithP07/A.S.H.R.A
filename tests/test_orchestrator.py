from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ashra.api import create_app
from ashra.chat import ChatService
from ashra.db import Database
from ashra.notes import Note
from ashra.orchestrator import Orchestrator
from ashra.providers import ToolCall, ToolTurn
from ashra.remember import RememberResult
from ashra.tools import Permission, build_default_registry


def make_note() -> Note:
    text = "Our pricing strategy is value based."
    return Note(
        id=1,
        label="Pricing Strategy",
        title="Pricing Strategy",
        group="Strategy",
        path="pricing.md",
        excerpt=text,
        text=text,
        words=len(text.split()),
    )


def final_turn(text: str = "Pricing is value based, sir.") -> ToolTurn:
    return ToolTurn(
        text=text,
        calls=(),
        assistant_message={"role": "assistant", "content": text},
    )


def call_turn(*calls: ToolCall) -> ToolTurn:
    return ToolTurn(
        text="",
        calls=tuple(calls),
        assistant_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in calls
            ],
        },
    )


class ScriptedToolProvider:
    tool_schema_style = "openai"

    def __init__(self, turns: list[ToolTurn] | None = None) -> None:
        self.turns = list(turns or [])
        self.calls = 0
        self.schemas: list[dict] = []
        self.messages: list[list[dict]] = []

    def push(self, turn: ToolTurn) -> None:
        self.turns.append(turn)

    def complete_turn(self, system, messages, tools):
        self.calls += 1
        self.schemas = tools
        self.messages.append(messages)
        if not self.turns:
            return final_turn()
        return self.turns.pop(0)

    def tool_messages(self, turn, outcomes):
        return [
            {
                "role": "tool",
                "tool_call_id": outcome.call_id,
                "content": json.dumps(
                    {"ok": outcome.ok, "result": outcome.result, "error": outcome.error},
                    default=str,
                ),
            }
            for outcome in outcomes
        ]


class PlainProvider:
    """A provider that only implements the original complete() contract."""

    def __init__(self, answer: str = "Plain answer.") -> None:
        self.answer = answer
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        return self.answer


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "notes").mkdir()
        (self.base / "notes" / "pricing.md").write_text(
            "# Pricing Strategy\n\nOur pricing strategy is value based.\n",
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
        self.provider = ScriptedToolProvider()
        self.chat = ChatService(
            self.base,
            notes_loader=lambda: [make_note()],
            provider_factory=lambda _config: self.provider,
        )
        self.registry = build_default_registry(self.base)
        self.orchestrator = Orchestrator(self.chat, self.registry, self.db)

    # ------------------------------------------------------------------
    # read tools
    # ------------------------------------------------------------------
    def test_read_tool_runs_and_the_answer_is_persisted(self) -> None:
        self.provider.push(
            call_turn(ToolCall("c1", "fs_search", {"query": "pricing"}))
        )
        self.provider.push(final_turn())

        events = list(
            self.orchestrator.stream(
                "What is our pricing strategy?",
                granted=Permission.READ,
            )
        )
        names = [event["event"] for event in events]
        self.assertIn("tool_call", names)
        self.assertIn("tool_result", names)
        self.assertIn("token", names)
        self.assertEqual(names[-1], "done")

        result = next(event for event in events if event["event"] == "tool_result")
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["results"][0]["path"], "pricing.md")

        done = events[-1]
        self.assertEqual(done["status"], "ok")
        self.assertEqual(done["answer"], "Pricing is value based, sir.")
        self.assertEqual(done["nodes"], [1])
        self.assertEqual(done["steps"], 2)

        history = self.chat.sessions.history(done["session_id"])
        self.assertEqual(history[0]["content"], "What is our pricing strategy?")
        self.assertEqual(history[-1]["role"], "assistant")

    def test_tool_schemas_are_sent_to_the_provider(self) -> None:
        self.provider.push(final_turn())
        list(self.orchestrator.stream("What is our pricing strategy?"))
        names = {schema["function"]["name"] for schema in self.provider.schemas}
        self.assertIn("fs_search", names)
        self.assertNotIn("vault_remember", names)

    def test_tool_schemas_support_both_provider_styles(self) -> None:
        spec = self.registry.get("fs_read")
        anthropic = spec.provider_schema("anthropic")
        openai = spec.provider_schema("openai")

        self.assertEqual(anthropic["name"], "fs_read")
        self.assertIn("input_schema", anthropic)
        self.assertEqual(openai["type"], "function")
        self.assertEqual(openai["function"]["parameters"], spec.input_schema)

    def test_openai_tool_calls_are_parsed(self) -> None:
        from ashra.providers import _parse_openai_calls

        calls = _parse_openai_calls(
            [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "fs_search",
                        "arguments": json.dumps({"query": "pricing"}),
                    },
                },
                {
                    "type": "function",
                    "function": {"name": "fs_list", "arguments": "not json"},
                },
            ]
        )
        self.assertEqual(calls[0].name, "fs_search")
        self.assertEqual(calls[0].arguments, {"query": "pricing"})
        self.assertEqual(calls[1].name, "fs_list")
        self.assertIn("_raw_arguments", calls[1].arguments)

    # ------------------------------------------------------------------
    # approvals
    # ------------------------------------------------------------------
    def _start_write(self) -> dict:
        self.provider.push(
            call_turn(ToolCall("c1", "vault_remember", {"text": "We ship on Fridays."}))
        )
        events = list(
            self.orchestrator.stream(
                "Save a note about our pricing strategy.",
                granted=Permission.ACT,
            )
        )
        approval = next(event for event in events if event["event"] == "approval_required")
        done = events[-1]
        self.assertEqual(done["status"], "awaiting_approval")
        self.assertEqual(approval["approval"]["tool"], "vault_remember")
        return approval["approval"]

    def test_write_tool_waits_for_approval(self) -> None:
        with patch("ashra.tools.remember_capture") as mocked:
            approval = self._start_write()
        mocked.assert_not_called()

        row = self.db.get_approval(approval["id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.db.count_approvals(), 1)

    def test_approval_runs_the_tool_and_finishes(self) -> None:
        with patch(
            "ashra.tools.remember_capture",
            return_value=RememberResult(
                title="Ship Day",
                path="captures/ship-day.md",
                node=2,
                related=None,
                graph={"nodes": [], "links": []},
                reply="Noted, sir.",
            ),
        ) as mocked:
            approval = self._start_write()
            self.provider.push(final_turn("Noted, sir. Fridays it is."))
            result = self.orchestrator.resume_result(approval["id"], approve=True)

        mocked.assert_called_once()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.answer, "Noted, sir. Fridays it is.")
        self.assertEqual(self.db.get_approval(approval["id"])["status"], "completed")

    def test_denied_approval_does_not_run_the_tool(self) -> None:
        with patch("ashra.tools.remember_capture") as mocked:
            approval = self._start_write()
            result = self.orchestrator.resume_result(approval["id"], approve=False)

        mocked.assert_not_called()
        self.assertEqual(result.status, "denied")
        self.assertIn("did not run", result.answer)
        self.assertEqual(self.db.get_approval(approval["id"])["status"], "denied")

    def test_approval_cannot_be_decided_twice(self) -> None:
        with patch("ashra.tools.remember_capture") as mocked:
            approval = self._start_write()
            self.orchestrator.resume_result(approval["id"], approve=False)
            with self.assertRaises(ValueError):
                self.orchestrator.resume_result(approval["id"], approve=False)
        mocked.assert_not_called()

    # ------------------------------------------------------------------
    # bounds and fallbacks
    # ------------------------------------------------------------------
    def test_loop_stops_after_max_steps(self) -> None:
        bounded = Orchestrator(self.chat, self.registry, self.db, max_steps=2)
        for _ in range(5):
            self.provider.push(
                call_turn(ToolCall("c1", "fs_list", {"relative_path": ""}))
            )

        events = list(bounded.stream("What is our pricing strategy?"))
        self.assertEqual(events[-1]["status"], "ok")
        self.assertIn("stopped after several tool steps", events[-1]["answer"])
        self.assertEqual(self.provider.calls, 2)

    def test_provider_without_tools_still_answers(self) -> None:
        plain = PlainProvider("Plain answer.")
        chat = ChatService(
            self.base,
            notes_loader=lambda: [make_note()],
            provider_factory=lambda _config: plain,
        )
        orchestrator = Orchestrator(chat, self.registry, self.db)
        events = list(orchestrator.stream("What is our pricing strategy?"))

        names = [event["event"] for event in events]
        self.assertNotIn("tool_call", names)
        self.assertEqual(events[-1]["answer"], "Plain answer.")
        self.assertEqual(plain.calls, 1)


class OrchestratorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "notes").mkdir()
        (self.base / "notes" / "pricing.md").write_text(
            "# Pricing Strategy\n\nOur pricing strategy is value based.\n",
            encoding="utf-8",
        )
        (self.base / "viewer").mkdir()
        (self.base / "viewer" / "index.html").write_text("<h1>A.S.H.R.A</h1>", encoding="utf-8")
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
        self.provider = ScriptedToolProvider()
        self.chat = ChatService(
            self.base,
            notes_loader=lambda: [make_note()],
            provider_factory=lambda _config: self.provider,
        )
        self.app = create_app(self.base, chat=self.chat, database=self.db)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_chat_returns_awaiting_approval_and_resumes(self) -> None:
        self.provider.push(
            call_turn(ToolCall("c1", "vault_remember", {"text": "We ship on Fridays."}))
        )
        with patch(
            "ashra.tools.remember_capture",
            return_value=RememberResult(
                title="Ship Day",
                path="captures/ship-day.md",
                node=2,
                related=None,
                graph={"nodes": [], "links": []},
                reply="Noted, sir.",
            ),
        ) as mocked:
            response = self.client.post(
                "/chat",
                json={
                    "question": "Save a note about our pricing strategy.",
                    "permission": "act",
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["status"], "awaiting_approval")
            approval_id = payload["pending_approval"]["id"]

            pending = self.client.get("/approvals?status=pending").json()["approvals"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["id"], approval_id)

            self.provider.push(final_turn("Noted, sir."))
            decided = self.client.post(
                f"/approvals/{approval_id}",
                json={"approve": True},
            )
            self.assertEqual(decided.status_code, 200)
            self.assertEqual(decided.json()["status"], "ok")
            self.assertEqual(decided.json()["answer"], "Noted, sir.")

        mocked.assert_called_once()

    def test_stream_emits_approval_events(self) -> None:
        self.provider.push(
            call_turn(ToolCall("c1", "vault_remember", {"text": "We ship on Fridays."}))
        )
        response = self.client.post(
            "/chat/stream",
            json={
                "question": "Save a note about our pricing strategy.",
                "permission": "act",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: approval_required", response.text)
        self.assertIn("awaiting_approval", response.text)

    def test_unknown_approval_is_a_404(self) -> None:
        response = self.client.get("/approvals/nope")
        self.assertEqual(response.status_code, 404)

    def test_deciding_an_unknown_approval_is_a_400(self) -> None:
        response = self.client.post("/approvals/nope", json={"approve": True})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
