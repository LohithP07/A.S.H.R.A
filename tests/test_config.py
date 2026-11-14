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


class LaneConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def write(self, value: object) -> None:
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_local_lane_needs_no_key(self) -> None:
        self.write(
            {
                "provider": "ollama",
                "model": "qwen3.5:2b-q4_K_M",
                "local": {"model": "qwen3.5:2b-q4_K_M", "num_ctx": 4096},
            }
        )
        config = load_config(self.path)
        self.assertTrue(config.configured)
        self.assertEqual(config.for_local().provider, "ollama")
        self.assertEqual(config.for_local().api_key, "")
        self.assertEqual(config.for_local().local_num_ctx, 4096)

    def test_cloud_block_is_parsed_and_resolved_from_the_vault(self) -> None:
        self.write(
            {
                "provider": "ollama",
                "model": "qwen3.5:2b-q4_K_M",
                "cloud": {
                    "provider": "nvidia",
                    "model": "cloud-model",
                    "api_base": "https://example.invalid/v1",
                    "credential_ref": "ashra/cloud",
                },
                "privacy": {
                    "default_lane": "local",
                    "allow_cloud_for": ["public"],
                    "refuse": ["secret", "sensitive"],
                },
            }
        )
        config = load_config(self.path)

        self.assertTrue(config.cloud_configured)
        self.assertEqual(config.privacy_allow_cloud, ("public",))
        self.assertEqual(config.privacy_refuse, ("secret", "sensitive"))

        from ashra.credentials import MemoryVault

        cloud = config.for_cloud(MemoryVault({"ashra/cloud": "secret-value"}))
        self.assertEqual(cloud.provider, "nvidia")
        self.assertEqual(cloud.model, "cloud-model")
        self.assertEqual(cloud.api_key, "secret-value")

    def test_missing_vault_entry_leaves_cloud_unconfigured(self) -> None:
        self.write(
            {
                "provider": "ollama",
                "model": "local-model",
                "cloud": {
                    "provider": "nvidia",
                    "model": "cloud-model",
                    "credential_ref": "ashra/cloud",
                },
            }
        )
        config = load_config(self.path)
        self.assertTrue(config.cloud_configured)

        from ashra.credentials import MemoryVault

        cloud = config.for_cloud(MemoryVault())
        self.assertFalse(cloud.api_key.strip())

    def test_defaults_keep_private_content_local(self) -> None:
        self.write({"provider": "ollama", "model": "local-model"})
        config = load_config(self.path)
        self.assertEqual(config.privacy_default_lane, "local")
        self.assertEqual(config.privacy_allow_cloud, ("public", "internal"))
        self.assertEqual(config.privacy_refuse, ("secret",))


if __name__ == "__main__":
    unittest.main()
