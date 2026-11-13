from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ashra.config import PLACEHOLDER_API_KEY, load_config
from ashra.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def write(self, value: object) -> None:
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_missing_file_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_invalid_json_is_a_config_error(self) -> None:
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_non_object_json_is_a_config_error(self) -> None:
        self.write(["not", "an", "object"])
        with self.assertRaises(ConfigError):
            load_config(self.path)

    def test_values_are_bounded(self) -> None:
        self.write(
            {
                "api_key": "real-key",
                "model": "test-model",
                "provider": "anthropic",
                "max_notes": 99,
                "max_history": 1,
                "max_tokens": 99999,
                "temperature": 2.0,
                "note_chars": 1,
            }
        )

        config = load_config(self.path)

        self.assertTrue(config.configured)
        self.assertEqual(config.max_notes, 12)
        self.assertEqual(config.max_history, 2)
        self.assertEqual(config.max_tokens, 4096)
        self.assertEqual(config.temperature, 1.0)
        self.assertEqual(config.note_chars, 400)

    def test_placeholder_key_is_not_configured(self) -> None:
        self.write({"api_key": PLACEHOLDER_API_KEY, "model": "test-model"})
        config = load_config(self.path)
        self.assertFalse(config.configured)

    def test_put_prefix_is_not_configured(self) -> None:
        self.write({"api_key": "PUT-YOUR-KEY-HERE", "model": "test-model"})
        config = load_config(self.path)
        self.assertFalse(config.configured)


if __name__ == "__main__":
    unittest.main()
