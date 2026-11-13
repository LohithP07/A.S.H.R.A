from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ashra.chat import ChatService
from ashra.errors import NotConfiguredError
from ashra.notes import Note


def make_note(
    note_id: int,
    title: str,
    text: str,
    *,
    path: str | None = None,
    group: str = "General",
    created: datetime | None = None,
) -> Note:
    return Note(
        id=note_id,
        label=title,
        title=title,
        group=group,
        path=path or f"{title.lower().replace(' ', '-')}.md",
        excerpt=text[:100],
        text=text,
        words=len(text.split()),
        created=created,
    )


class FakeProvider:
    def __init__(self, answer: str = "Grounded answer.") -> None:
        self.answer = answer
        self.calls = 0
        self.system = ""
        self.messages: list[dict[str, str]] = []

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        self.system = system
        self.messages = messages
        return self.answer


class ChatServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config_path = self.base / "config.json"
        self.provider = FakeProvider()

    def write_config(self, *, configured: bool = True) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "api_key": "test-key" if configured else "PUT-YOUR-KEY-HERE",
                    "model": "test-model",
                    "provider": "anthropic",
                    "max_notes": 3,
                }
            ),
            encoding="utf-8",
        )

    def service(self, notes: list[Note]) -> ChatService:
        return ChatService(
            self.base,
            notes_loader=lambda: notes,
            provider_factory=lambda _config: self.provider,
        )

    def test_small_talk_works_without_a_configured_key(self) -> None:
        self.write_config(configured=False)
        result = self.service([]).ask("hello")
        self.assertIn("sir", result.answer.lower())
        self.assertEqual(result.nodes, [])
        self.assertEqual(self.provider.calls, 0)

    def test_real_question_requires_a_key(self) -> None:
        self.write_config(configured=False)
        with self.assertRaises(NotConfiguredError):
            self.service([]).ask("What is our pricing strategy?")

    def test_real_question_calls_the_provider_with_sources(self) -> None:
        self.write_config()
        note = make_note(1, "Pricing Strategy", "Our pricing strategy is value based.")
        result = self.service([note]).ask("What is our pricing strategy?")

        self.assertEqual(result.answer, "Grounded answer.")
        self.assertEqual(result.nodes, [1])
        self.assertEqual(result.sources[0]["title"], "Pricing Strategy")
        self.assertTrue(result.session_id)
        self.assertIn("Pricing Strategy", self.provider.system)
        self.assertEqual(self.provider.messages[-1]["content"], "What is our pricing strategy?")

    def test_no_match_does_not_call_the_provider(self) -> None:
        self.write_config()
        note = make_note(1, "Pricing Strategy", "Our pricing strategy is value based.")
        result = self.service([note]).ask("quantum entanglement")

        self.assertEqual(result.answer, "I don't have a note that covers that.")
        self.assertEqual(result.nodes, [])
        self.assertEqual(self.provider.calls, 0)

    def test_stream_emits_sources_tokens_and_done(self) -> None:
        self.write_config()
        note = make_note(1, "Pricing Strategy", "Our pricing strategy is value based.")
        events = list(self.service([note]).stream("What is our pricing strategy?"))

        names = [event["event"] for event in events]
        self.assertEqual(names[0], "start")
        self.assertIn("sources", names)
        self.assertIn("token", names)
        self.assertEqual(names[-1], "done")

        answer = "".join(
            str(event["text"]) for event in events if event["event"] == "token"
        )
        self.assertEqual(answer.strip(), "Grounded answer.")
        self.assertEqual(events[-1]["nodes"], [1])
        self.assertEqual(self.provider.calls, 1)

    def test_stream_small_talk_works_without_a_configured_key(self) -> None:
        self.write_config(configured=False)
        events = list(self.service([]).stream("hello"))

        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[-1]["event"], "done")
        self.assertIn("sir", str(events[-1]["answer"]).lower())
        self.assertEqual(self.provider.calls, 0)

    def test_stream_no_match_does_not_call_the_provider(self) -> None:
        self.write_config()
        note = make_note(1, "Pricing Strategy", "Our pricing strategy is value based.")
        events = list(self.service([note]).stream("quantum entanglement"))

        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(events[-1]["answer"], "I don't have a note that covers that.")
        self.assertEqual(self.provider.calls, 0)

    def test_stream_requires_a_key_for_a_real_question(self) -> None:
        self.write_config(configured=False)
        with self.assertRaises(NotConfiguredError):
            list(self.service([]).stream("What is our pricing strategy?"))

    def test_capture_recall_bypasses_the_provider(self) -> None:
        self.write_config(configured=False)
        note = make_note(
            1,
            "Spring Blend",
            "Use the new Ethiopian bean.",
            path="captures/spring-blend.md",
            group="Captures",
            created=datetime.now().astimezone(),
        )

        result = self.service([note]).ask("What did I capture this week?")

        self.assertIn("Spring Blend", result.answer)
        self.assertEqual(result.nodes, [1])
        self.assertEqual(self.provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
