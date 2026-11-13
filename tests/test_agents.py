from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ashra.agents import (
    AgentRegistry,
    AgentSpec,
    SubagentRunner,
    build_default_agents,
    build_spawn_tool,
    lower_permission,
    tool_context,
)
from ashra.api import create_app
from ashra.chat import ChatService
from ashra.db import Database
from ashra.errors import ToolInputError, ToolNotFound
from ashra.notes import Note
from ashra.providers import ToolCall, ToolTurn
from ashra.remember import RememberResult
from ashra.tools import Permission, ToolRegistry, ToolSpec, build_default_registry


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
        self.schemas: list[list[dict]] = []
        self.messages: list[list[dict]] = []

    def push(self, turn: ToolTurn) -> None:
        self.turns.append(turn)

    def complete_turn(self, system, messages, tools):
        self.calls += 1
        self.schemas.append(tools)
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
    def __init__(self, answer: str = "Plain answer.") -> None:
        self.answer = answer
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        return self.answer


class BrokenProvider:
    def complete_turn(self, system, messages, tools):
        raise RuntimeError("provider exploded")


def schema_names(schemas: list[dict]) -> set[str]:
    return {item["function"]["name"] for item in schemas}


class AgentRegistryTests(unittest.TestCase):
    def test_default_pack_has_three_specialists(self) -> None:
        registry = AgentRegistry(build_default_agents())
        self.assertEqual(registry.names(), ["operator", "researcher", "writer"])
        self.assertEqual(registry.get("researcher").permission, Permission.READ)
        self.assertEqual(registry.get("writer").permission, Permission.DRAFT)
        self.assertEqual(registry.get("operator").permission, Permission.ACT)
        self.assertEqual(len(registry), 3)

    def test_unknown_agent_is_not_found(self) -> None:
        registry = AgentRegistry(build_default_agents())
        with self.assertRaises(ToolNotFound):
            registry.get("nope")

    def test_duplicate_and_bad_names_are_rejected(self) -> None:
        registry = AgentRegistry([build_default_agents()[0]])
        with self.assertRaises(ValueError):
            registry.register(build_default_agents()[0])
        with self.assertRaises(ValueError):
            registry.register(AgentSpec(name="Bad Name", description="", instructions=""))

    def test_lower_permission_picks_the_weaker_tier(self) -> None:
        self.assertIs(lower_permission(Permission.ACT, Permission.READ), Permission.READ)
        self.assertIs(lower_permission(Permission.READ, Permission.ADMIN), Permission.READ)
        self.assertIs(lower_permission(Permission.DRAFT, Permission.DRAFT), Permission.DRAFT)

    def test_allowlist_supports_globs(self) -> None:
        spec = AgentSpec(
            name="scout",
            description="",
            instructions="",
            tools=("fs_*", "*_search"),
        )
        self.assertTrue(spec.allows_tool("fs_read"))
        self.assertTrue(spec.allows_tool("gmail_search"))
        self.assertFalse(spec.allows_tool("vault_remember"))
        empty = AgentSpec(name="empty", description="", instructions="", tools=())
        self.assertFalse(empty.allows_tool("fs_read"))


class SubagentRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "notes").mkdir()
        (self.base / "notes" / "pricing.md").write_text(
            "# Pricing Strategy\n\nOur pricing strategy is value based.\n",
            encoding="utf-8",
        )
        self.provider = ScriptedToolProvider()
        self.registry = build_default_registry(self.base)
        self.audit: list[dict] = []

    def runner(self, provider=None, *, max_steps: int = 4) -> SubagentRunner:
        return SubagentRunner(
            self.registry,
            lambda: provider or self.provider,
            audit=self.audit.append,
            max_steps=max_steps,
        )

    def test_researcher_runs_a_read_tool_and_returns_a_summary(self) -> None:
        self.provider.push(call_turn(ToolCall("c1", "fs_search", {"query": "pricing"})))
        self.provider.push(final_turn("Pricing is value based."))

        result = self.runner().run(
            AgentRegistry(build_default_agents()).get("researcher"),
            "Find our pricing strategy.",
            granted=Permission.READ,
            request_id="req-1",
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.agent, "researcher")
        self.assertEqual(result.permission, Permission.READ)
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.tools[0]["name"], "fs_search")
        self.assertTrue(result.tools[0]["ok"])
        self.assertIn("fs_search", schema_names(self.provider.schemas[0]))

        self.assertEqual(len(self.audit), 1)
        self.assertEqual(self.audit[0]["agent"], "researcher")
        self.assertEqual(self.audit[0]["status"], "ok")
        self.assertEqual(self.audit[0]["permission"], "read")
        self.assertEqual(self.audit[0]["request_id"], "req-1")

    def test_child_cannot_exceed_the_parent_permission(self) -> None:
        self.provider.push(
            call_turn(ToolCall("c1", "vault_remember", {"text": "We ship on Fridays."}))
        )
        self.provider.push(final_turn("I cannot approve that."))

        with patch("ashra.tools.remember_capture") as mocked:
            result = self.runner().run(
                AgentRegistry(build_default_agents()).get("operator"),
                "Save a note about pricing.",
                granted=Permission.READ,
            )

        mocked.assert_not_called()
        self.assertEqual(result.permission, Permission.READ)
        self.assertFalse(result.tools[0]["ok"])
        self.assertIn("approval", result.tools[0]["error"].lower())
        self.assertNotIn("vault_remember", schema_names(self.provider.schemas[0]))

    def test_approval_gated_tool_is_refused_even_for_an_act_parent(self) -> None:
        self.provider.push(
            call_turn(ToolCall("c1", "vault_remember", {"text": "We ship on Fridays."}))
        )
        self.provider.push(final_turn("Please approve that in the parent."))

        with patch("ashra.tools.remember_capture") as mocked:
            result = self.runner().run(
                AgentRegistry(build_default_agents()).get("operator"),
                "Save a note about pricing.",
                granted=Permission.ACT,
            )

        mocked.assert_not_called()
        self.assertEqual(result.permission, Permission.ACT)
        self.assertFalse(result.tools[0]["ok"])
        self.assertIn("parent", result.tools[0]["error"].lower())

    def test_allowlist_hides_tools_outside_the_agent_scope(self) -> None:
        narrow = AgentSpec(
            name="narrow",
            description="Only searches.",
            instructions="Search.",
            permission=Permission.READ,
            tools=("fs_search",),
        )
        self.provider.push(
            call_turn(ToolCall("c1", "fs_read", {"relative_path": "pricing.md"}))
        )
        self.provider.push(final_turn("Only search is allowed."))

        result = self.runner().run(narrow, "Read the pricing note.", granted=Permission.READ)

        self.assertEqual(schema_names(self.provider.schemas[0]), {"fs_search"})
        self.assertFalse(result.tools[0]["ok"])
        self.assertIn("allowlist", result.tools[0]["error"])

    def test_spawn_agent_is_never_offered_to_a_child(self) -> None:
        self.registry.register(
            ToolSpec(
                name="spawn_agent",
                description="dummy",
                handler=lambda arguments: {},
                permission=Permission.READ,
            )
        )
        self.provider.push(final_turn("Done."))

        self.runner().run(
            AgentRegistry(build_default_agents()).get("researcher"),
            "Do the task.",
            granted=Permission.READ,
        )

        self.assertNotIn("spawn_agent", schema_names(self.provider.schemas[0]))

    def test_step_limit_stops_a_looping_child(self) -> None:
        agent = AgentSpec(
            name="looper",
            description="",
            instructions="",
            permission=Permission.READ,
            max_steps=2,
        )
        for _ in range(5):
            self.provider.push(call_turn(ToolCall("c1", "fs_list", {})))

        result = self.runner().run(agent, "Loop forever.", granted=Permission.READ)

        self.assertEqual(result.status, "limit")
        self.assertEqual(result.steps, 2)
        self.assertEqual(len(self.provider.schemas), 2)

    def test_provider_without_tools_still_returns_an_answer(self) -> None:
        result = self.runner(PlainProvider("Draft text.")).run(
            AgentRegistry(build_default_agents()).get("writer"),
            "Draft a note about pricing.",
            granted=Permission.DRAFT,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.answer, "Draft text.")
        self.assertEqual(result.steps, 0)

    def test_provider_failure_becomes_a_structured_error(self) -> None:
        result = self.runner(BrokenProvider()).run(
            AgentRegistry(build_default_agents()).get("researcher"),
            "Find pricing.",
            granted=Permission.READ,
        )

        self.assertEqual(result.status, "error")
        self.assertIn("provider exploded", result.answer)
        self.assertEqual(self.audit[0]["status"], "error")

    def test_empty_task_is_rejected(self) -> None:
        with self.assertRaises(ToolInputError):
            self.runner().run(
                AgentRegistry(build_default_agents()).get("researcher"),
                "   ",
            )

    def test_spawn_tool_reads_the_parent_context(self) -> None:
        self.provider.push(final_turn("Pricing is value based."))
        agents = AgentRegistry(build_default_agents())
        spawn = build_spawn_tool(self.runner(), agents)
        self.registry.register(spawn)

        self.assertEqual(
            spawn.input_schema["properties"]["agent"]["enum"],
            ["operator", "researcher", "writer"],
        )

        with tool_context(Permission.READ, "req-9"):
            payload = self.registry.invoke(
                "spawn_agent",
                {"agent": "researcher", "task": "Find pricing."},
                granted=Permission.READ,
                request_id="req-9",
            )

        self.assertEqual(payload["agent"], "researcher")
        self.assertEqual(payload["permission"], "read")
        self.assertEqual(payload["answer"], "Pricing is value based.")
        self.assertEqual(self.audit[0]["request_id"], "req-9")


class AgentApiTests(unittest.TestCase):
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
        (self.base / "viewer" / "index.html").write_text(
            "<h1>A.S.H.R.A</h1>", encoding="utf-8"
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
        self.app = create_app(self.base, chat=self.chat, database=self.db)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_agents_endpoints_and_status(self) -> None:
        payload = self.client.get("/agents").json()
        names = [item["name"] for item in payload["agents"]]
        self.assertEqual(names, ["operator", "researcher", "writer"])

        self.assertEqual(self.client.get("/agents/researcher").status_code, 200)
        self.assertEqual(self.client.get("/agents/nope").status_code, 404)
        self.assertEqual(self.client.get("/status").json()["agents"], 3)

    def test_spawn_agent_is_listed_for_read_callers(self) -> None:
        payload = self.client.get("/tools?permission=read").json()
        self.assertIn("spawn_agent", [item["name"] for item in payload["tools"]])

    def test_chat_delegates_to_a_researcher(self) -> None:
        self.provider.push(
            call_turn(
                ToolCall(
                    "c1",
                    "spawn_agent",
                    {"agent": "researcher", "task": "Find our pricing strategy."},
                )
            )
        )
        self.provider.push(final_turn("Pricing is value based."))
        self.provider.push(final_turn("The researcher found that pricing is value based."))

        response = self.client.post(
            "/chat",
            json={
                "question": "Research our pricing strategy and answer.",
                "permission": "read",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["answer"], "The researcher found that pricing is value based.")
        self.assertEqual(payload["tools"][0]["name"], "spawn_agent")
        self.assertTrue(payload["tools"][0]["ok"])

        events = self.client.get("/audit?event=agent&limit=10").json()["events"]
        self.assertTrue(any(event["action"] == "researcher" for event in events), events)

    def test_tools_invoke_can_spawn_an_agent(self) -> None:
        self.provider.push(final_turn("Researcher answer."))

        response = self.client.post(
            "/tools/invoke",
            json={
                "name": "spawn_agent",
                "arguments": {"agent": "researcher", "task": "Find pricing."},
                "permission": "read",
            },
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["agent"], "researcher")
        self.assertEqual(result["permission"], "read")

    def test_invoked_child_still_respects_the_parent_permission(self) -> None:
        self.provider.push(
            call_turn(ToolCall("c1", "vault_remember", {"text": "We ship on Fridays."}))
        )
        self.provider.push(final_turn("I cannot approve that."))

        with patch("ashra.tools.remember_capture") as mocked:
            response = self.client.post(
                "/tools/invoke",
                json={
                    "name": "spawn_agent",
                    "arguments": {"agent": "operator", "task": "Save a note."},
                    "permission": "read",
                },
            )

        mocked.assert_not_called()
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["permission"], "read")
        self.assertFalse(result["tools"][0]["ok"])
        self.assertIn("approval", result["tools"][0]["error"].lower())


if __name__ == "__main__":
    unittest.main()
