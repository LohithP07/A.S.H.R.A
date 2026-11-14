from __future__ import annotations

import json
import unittest
from dataclasses import replace

from ashra.config import AppConfig
from ashra.errors import ProviderError
from ashra.ollama import OllamaProvider, _base_url


def local_config(**overrides) -> AppConfig:
    base = AppConfig(
        provider="ollama",
        model="qwen3.5:2b-q4_K_M",
        api_base="http://127.0.0.1:11434",
        local_num_ctx=8192,
        local_timeout=30,
        max_tokens=512,
    )
    return replace(base, **overrides) if overrides else base


class StubProvider(OllamaProvider):
    """Capture the request instead of making it."""

    def __init__(self, config: AppConfig, response: dict, error: Exception | None = None):
        super().__init__(config)
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict | None, int]] = []

    def _request(self, path: str, payload: dict | None, timeout: int) -> dict:
        self.calls.append((path, payload, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class BaseUrlTests(unittest.TestCase):
    def test_accepts_host_and_v1(self) -> None:
        self.assertEqual(_base_url("http://127.0.0.1:11434"), "http://127.0.0.1:11434")
        self.assertEqual(
            _base_url("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434",
        )
        self.assertEqual(
            _base_url("http://127.0.0.1:11434/api/chat"),
            "http://127.0.0.1:11434",
        )

    def test_empty_falls_back_to_loopback(self) -> None:
        self.assertEqual(_base_url(""), "http://127.0.0.1:11434")


class ChatPayloadTests(unittest.TestCase):
    def test_thinking_is_disabled_and_context_is_explicit(self) -> None:
        provider = StubProvider(
            local_config(),
            {"message": {"content": "hello"}},
        )
        self.assertEqual(provider.complete("sys", [{"role": "user", "content": "hi"}]), "hello")

        _path, payload, timeout = provider.calls[0]
        self.assertEqual(payload["model"], "qwen3.5:2b-q4_K_M")
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertEqual(payload["options"]["num_predict"], 512)
        self.assertFalse(payload["stream"])
        self.assertNotIn("format", payload)
        self.assertEqual(timeout, 30)
        self.assertEqual(payload["messages"][0]["role"], "system")

    def test_json_format_is_requested_for_structured_tasks(self) -> None:
        provider = StubProvider(
            local_config(),
            {"message": {"content": '{"class":"reply_needed"}'}},
        )
        parsed = provider.complete_json("sys", [{"role": "user", "content": "classify"}])

        self.assertEqual(parsed, {"class": "reply_needed"})
        _path, payload, _timeout = provider.calls[0]
        self.assertEqual(payload["format"], "json")

    def test_thinking_can_be_enabled_by_configuration(self) -> None:
        provider = StubProvider(
            local_config(local_think=True),
            {"message": {"content": "hello"}},
        )
        provider.complete("sys", [{"role": "user", "content": "hi"}])
        self.assertTrue(provider.calls[0][1]["think"])

    def test_config_can_default_to_json(self) -> None:
        provider = StubProvider(
            local_config(local_format="json"),
            {"message": {"content": "{}"}},
        )
        provider.complete("sys", [{"role": "user", "content": "hi"}])
        self.assertEqual(provider.calls[0][1]["format"], "json")

    def test_invalid_json_is_a_provider_error(self) -> None:
        provider = StubProvider(
            local_config(),
            {"message": {"content": "not json"}},
        )
        with self.assertRaises(ProviderError):
            provider.complete_json("sys", [{"role": "user", "content": "x"}])

    def test_empty_answer_is_a_provider_error(self) -> None:
        provider = StubProvider(local_config(), {"message": {"content": ""}})
        with self.assertRaises(ProviderError):
            provider.complete("sys", [{"role": "user", "content": "x"}])

    def test_length_stop_is_named_in_the_error(self) -> None:
        provider = StubProvider(
            local_config(),
            {"message": {"content": ""}, "done_reason": "length"},
        )
        with self.assertRaises(ProviderError) as caught:
            provider.complete("sys", [{"role": "user", "content": "x"}])
        self.assertIn("token budget", str(caught.exception))

    def test_tool_calls_are_parsed(self) -> None:
        provider = StubProvider(
            local_config(),
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "search_notes", "arguments": {"q": "deck"}}}
                    ],
                }
            },
        )
        turn = provider.complete_turn("sys", [{"role": "user", "content": "x"}], [])
        self.assertEqual(turn.calls[0].name, "search_notes")
        self.assertEqual(turn.calls[0].arguments, {"q": "deck"})


class HealthTests(unittest.TestCase):
    def test_reports_ok_when_the_model_is_installed(self) -> None:
        provider = StubProvider(
            local_config(),
            {"models": [{"name": "qwen3.5:2b-q4_K_M"}, {"name": "qwen3.5:2b"}]},
        )
        health = provider.health()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["model"], "qwen3.5:2b-q4_K_M")

    def test_reports_a_missing_model_by_name(self) -> None:
        provider = StubProvider(local_config(), {"models": [{"name": "qwen3.5:2b"}]})
        health = provider.health()
        self.assertEqual(health["status"], "model_missing")
        self.assertIn("ollama pull", health["detail"])

    def test_reports_a_down_server(self) -> None:
        provider = StubProvider(
            local_config(),
            {},
            error=ProviderError("The local model is not reachable (refused)."),
        )
        health = provider.health()
        self.assertEqual(health["status"], "down")
        self.assertIn("not reachable", health["detail"])

    def test_latest_tag_is_accepted(self) -> None:
        provider = StubProvider(local_config(), {"models": [{"name": "qwen3.5:2b-q4_K_M:latest"}]})
        self.assertEqual(provider.health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
