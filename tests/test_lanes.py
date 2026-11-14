from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ashra.chat import REFUSAL_ANSWER, ChatService
from ashra.credentials import MemoryVault
from ashra.db import Database
from ashra.errors import ProviderError
from ashra.notes import Note
from ashra.orchestrator import Orchestrator
from ashra.providers import ToolTurn
from ashra.tools import ToolRegistry


def make_note(text: str, note_id: int = 1, title: str = "Pricing Strategy") -> Note:
    return Note(
        id=note_id,
        label=title,
        title=title,
        group="General",
        path=f"{title.lower().replace(' ', '-')}.md",
        excerpt=text[:100],
        text=text,
        words=len(text.split()),
        created=None,
    )


class RecordingProvider:
    tool_schema_style = "openai"

    def __init__(self, config, factory: "RecordingFactory") -> None:
        self.config = config
        self.factory = factory

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        self.factory.calls.append(self.config.provider)
        self.factory.keys.append(self.config.api_key)
        self.factory.models.append(self.config.model)
        if self.factory.fail_provider == self.config.provider:
            raise ProviderError(f"{self.config.provider} is unavailable")
        return self.factory.answer

    def complete_turn(self, system, messages, tools):
        answer = self.complete(system, messages)
        return ToolTurn(
            text=answer,
            calls=(),
            assistant_message={"role": "assistant", "content": answer},
        )

    def tool_messages(self, turn, outcomes):
        return []


class RecordingFactory:
    def __init__(self, answer: str = "Grounded answer.", fail_provider: str | None = None):
        self.answer = answer
        self.fail_provider = fail_provider
        self.calls: list[str] = []
        self.keys: list[str] = []
        self.models: list[str] = []

    def __call__(self, config):
        return RecordingProvider(config, self)


class LaneIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config_path = self.base / "config.json"
        self.vault = MemoryVault({"ashra/cloud": "cloud-secret"})
        self.write_config()

    def write_config(self) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "provider": "ollama",
                    "model": "qwen3.5:2b-q4_K_M",
                    "model_note": "local default",
                    "local": {
                        "model": "qwen3.5:2b-q4_K_M",
                        "api_base": "http://127.0.0.1:11434",
                        "num_ctx": 4096,
                        "think": False,
                        "timeout_s": 30,
                    },
                    "cloud": {
                        "provider": "nvidia",
                        "model": "cloud-model",
                        "api_base": "https://example.invalid/v1",
                        "credential_ref": "ashra/cloud",
                    },
                    "privacy": {
                        "default_lane": "local",
                        "allow_cloud_for": ["public", "internal"],
                        "refuse": ["secret"],
                    },
                    "max_notes": 3,
                    "max_memories": 0,
                }
            ),
            encoding="utf-8",
        )

    def service(self, notes: list[Note], factory: RecordingFactory, audit=None) -> ChatService:
        return ChatService(
            self.base,
            notes_loader=lambda: notes,
            provider_factory=factory,
            audit=audit,
            vault=self.vault,
        )

    # ------------------------------------------------------------------
    def test_private_note_is_answered_on_the_local_lane(self) -> None:
        factory = RecordingFactory()
        note = make_note(
            "Our pricing strategy is value based. Ask sarah@example.com for the deck."
        )
        result = self.service([note], factory).ask("What is our pricing strategy?")

        self.assertEqual(result.lane, "local")
        self.assertEqual(result.sensitivity, "private")
        self.assertEqual(factory.calls, ["ollama"])
        self.assertEqual(factory.keys, [""])
        self.assertEqual(self.vault.get("ashra/cloud"), "cloud-secret")

    def test_public_note_uses_cloud_with_the_vault_key(self) -> None:
        factory = RecordingFactory()
        note = make_note("Our pricing strategy is value based.")
        result = self.service([note], factory).ask("What is our pricing strategy?")

        self.assertEqual(result.lane, "cloud")
        self.assertEqual(factory.calls, ["nvidia"])
        self.assertEqual(factory.keys, ["cloud-secret"])
        self.assertEqual(factory.models, ["cloud-model"])

    def test_secret_content_is_refused_before_any_provider(self) -> None:
        factory = RecordingFactory()
        note = make_note("Our pricing strategy is value based. password: hunter2")
        result = self.service([note], factory).ask("What is our pricing strategy?")

        self.assertEqual(factory.calls, [])
        self.assertEqual(result.lane, "refusal")
        self.assertEqual(result.answer, REFUSAL_ANSWER)

    def test_local_failure_never_falls_back_to_cloud(self) -> None:
        factory = RecordingFactory(fail_provider="ollama")
        note = make_note("Our pricing strategy is value based. Ask sarah@example.com.")
        result = self.service([note], factory).ask("What is our pricing strategy?")

        self.assertEqual(factory.calls, ["ollama"])
        self.assertIn("did not fall back", result.answer)
        self.assertEqual(result.lane, "local")

    def test_cloud_failure_falls_back_to_local_with_a_warning(self) -> None:
        factory = RecordingFactory(fail_provider="nvidia")
        note = make_note("Our pricing strategy is value based.")
        result = self.service([note], factory).ask("What is our pricing strategy?")

        self.assertEqual(factory.calls, ["nvidia", "ollama"])
        self.assertIn("Answered locally", result.answer)
        self.assertEqual(result.lane, "cloud")

    def test_audit_records_lane_and_class(self) -> None:
        records: list[dict] = []
        factory = RecordingFactory()
        note = make_note("Our pricing strategy is value based. Ask sarah@example.com.")
        self.service([note], factory, audit=records.append).ask(
            "What is our pricing strategy?"
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["lane"], "local")
        self.assertEqual(records[0]["sensitivity"], "private")
        self.assertEqual(records[0]["status"], "ok")
        self.assertEqual(records[0]["model"], "qwen3.5:2b-q4_K_M")

    def test_refusal_is_audited_without_a_model(self) -> None:
        records: list[dict] = []
        factory = RecordingFactory()
        note = make_note("Our pricing strategy is value based. password: hunter2")
        self.service([note], factory, audit=records.append).ask(
            "What is our pricing strategy?"
        )

        self.assertEqual(records[0]["lane"], "refusal")
        self.assertEqual(records[0]["status"], "refused")
        self.assertEqual(records[0]["model"], "")

    def test_unconfigured_cloud_stays_on_the_local_lane(self) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "provider": "ollama",
                    "model": "qwen3.5:2b-q4_K_M",
                    "local": {"model": "qwen3.5:2b-q4_K_M"},
                    "max_notes": 3,
                    "max_memories": 0,
                }
            ),
            encoding="utf-8",
        )
        factory = RecordingFactory()
        note = make_note("Our pricing strategy is value based.")
        result = self.service([note], factory).ask("What is our pricing strategy?")

        self.assertEqual(factory.calls, ["ollama"])
        self.assertEqual(result.lane, "local")
        self.assertIn("not configured", result.route["reason"])

    def test_stream_done_event_reports_the_lane(self) -> None:
        factory = RecordingFactory()
        note = make_note("Our pricing strategy is value based. Ask sarah@example.com.")
        events = list(
            self.service([note], factory).stream("What is our pricing strategy?")
        )

        self.assertEqual(events[-1]["lane"], "local")
        self.assertEqual(events[-1]["sensitivity"], "private")


class OrchestratorLaneTests(unittest.TestCase):
    """The tool loop must obey the same boundary as the plain chat path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config_path = self.base / "config.json"
        self.vault = MemoryVault({"ashra/cloud": "cloud-secret"})
        self.config_path.write_text(
            json.dumps(
                {
                    "provider": "ollama",
                    "model": "qwen3.5:2b-q4_K_M",
                    "local": {"model": "qwen3.5:2b-q4_K_M", "num_ctx": 4096},
                    "cloud": {
                        "provider": "nvidia",
                        "model": "cloud-model",
                        "api_base": "https://example.invalid/v1",
                        "credential_ref": "ashra/cloud",
                    },
                    "max_notes": 3,
                    "max_memories": 0,
                }
            ),
            encoding="utf-8",
        )
        self.db = Database(self.base / "data" / "test.db")

    def orchestrator(self, note_text: str, factory: RecordingFactory) -> Orchestrator:
        chat = ChatService(
            self.base,
            notes_loader=lambda: [make_note(note_text)],
            provider_factory=factory,
            vault=self.vault,
        )
        return Orchestrator(chat, ToolRegistry(), self.db)

    def test_private_content_runs_the_tool_loop_locally(self) -> None:
        factory = RecordingFactory()
        note = "Our pricing strategy is value based. Ask sarah@example.com."
        result = self.orchestrator(note, factory).ask("What is our pricing strategy?")

        self.assertEqual(factory.calls, ["ollama"])
        self.assertEqual(result.model, "qwen3.5:2b-q4_K_M")
        self.assertEqual(result.status, "ok")

    def test_secret_content_never_reaches_the_tool_loop(self) -> None:
        factory = RecordingFactory()
        note = "Our pricing strategy is value based. password: hunter2"
        result = self.orchestrator(note, factory).ask("What is our pricing strategy?")

        self.assertEqual(factory.calls, [])
        self.assertEqual(result.answer, REFUSAL_ANSWER)

    def test_local_failure_degrades_without_the_cloud(self) -> None:
        factory = RecordingFactory(fail_provider="ollama")
        note = "Our pricing strategy is value based. Ask sarah@example.com."
        result = self.orchestrator(note, factory).ask("What is our pricing strategy?")

        self.assertEqual(factory.calls, ["ollama"])
        self.assertIn("did not fall back", result.answer)
        self.assertEqual(result.status, "degraded")


if __name__ == "__main__":
    unittest.main()
