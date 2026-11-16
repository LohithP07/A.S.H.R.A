"""The two-model catalog, the persisted selection, and the picker endpoints."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from ashra.api import create_app
from ashra.config import AppConfig, load_config
from ashra.credentials import MemoryVault
from ashra.db import Database
from ashra.errors import ConfigError
from ashra.models import CLOUD, LOCAL, catalog, find, selected, write_selection


def config_for(
    *,
    chat_model: str | None = "local",
    cloud: bool = True,
    local: bool = True,
) -> dict:
    raw: dict = {
        "provider": "ollama",
        "model": "qwen3.5:2b-q4_K_M",
        "max_notes": 3,
    }
    if local:
        raw["local"] = {
            "model": "qwen3.5:2b-q4_K_M",
            "api_base": "http://127.0.0.1:11434",
        }
    if cloud:
        raw["cloud"] = {
            "provider": "nvidia",
            "model": "nvidia/nemotron-3.5-super-vl-120b-a12b",
            "api_base": "https://example.invalid/v1",
            "credential_ref": "ashra/cloud",
        }
    if chat_model is not None:
        raw["chat"] = {"model": chat_model}
    return raw


class CatalogTests(unittest.TestCase):
    def test_default_selection_is_local(self) -> None:
        config = AppConfig()
        self.assertEqual(config.chat_model, "local")
        self.assertEqual(selected(config), LOCAL)

    def test_load_config_reads_the_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config_for(chat_model="cloud")), encoding="utf-8")
            self.assertEqual(load_config(path).chat_model, "cloud")

    def test_an_unknown_selection_falls_back_to_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config_for(chat_model="gpt-9")), encoding="utf-8")
            self.assertEqual(load_config(path).chat_model, "local")

    def test_missing_chat_block_defaults_to_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config_for(chat_model=None)), encoding="utf-8")
            self.assertEqual(load_config(path).chat_model, "local")

    def test_catalog_lists_both_models_with_their_own_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config_for()), encoding="utf-8")
            options = catalog(load_config(path), MemoryVault({"ashra/cloud": "k"}))

        self.assertEqual([option.id for option in options], [LOCAL, CLOUD])
        self.assertEqual(options[0].model, "qwen3.5:2b-q4_K_M")
        self.assertEqual(options[0].provider, "ollama")
        self.assertEqual(options[1].model, "nvidia/nemotron-3.5-super-vl-120b-a12b")
        self.assertEqual(options[1].provider, "nvidia")

    def test_cloud_is_unavailable_without_its_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config_for()), encoding="utf-8")
            options = catalog(load_config(path), MemoryVault({}))

        cloud = find(options, CLOUD)
        assert cloud is not None
        self.assertFalse(cloud.available)
        self.assertIn("credential", cloud.detail)
        # The local lane never needs a key.
        self.assertTrue(find(options, LOCAL).available)

    def test_a_cloud_block_without_a_model_is_unavailable(self) -> None:
        raw = config_for(cloud=False)
        raw["cloud"] = {"provider": "nvidia"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            options = catalog(load_config(path), MemoryVault({}))

        self.assertFalse(find(options, CLOUD).available)


class WriteSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.path = self.base / "config.json"

    def test_writing_preserves_every_other_key(self) -> None:
        raw = {
            "provider": "ollama",
            "model": "qwen3.5:2b-q4_K_M",
            "api_key": "do-not-lose-me",
            "chat": {"model": "local", "note": "keep this"},
            "privacy": {"allow_cloud_for": ["public"]},
            "connectors": {"gmail": {"enabled": True}},
        }
        self.path.write_text(json.dumps(raw), encoding="utf-8")

        self.assertEqual(write_selection(self.path, "cloud"), "cloud")

        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["chat"]["model"], "cloud")
        self.assertEqual(saved["chat"]["note"], "keep this")
        self.assertEqual(saved["api_key"], "do-not-lose-me")
        self.assertEqual(saved["privacy"], raw["privacy"])
        self.assertEqual(saved["connectors"], raw["connectors"])
        self.assertEqual(saved["provider"], "ollama")

    def test_writing_creates_a_chat_block_when_missing(self) -> None:
        self.path.write_text(
            json.dumps(config_for(chat_model=None)), encoding="utf-8"
        )
        write_selection(self.path, "cloud")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["chat"], {"model": "cloud"})

    def test_writing_leaves_no_temporary_file(self) -> None:
        self.path.write_text(json.dumps(config_for()), encoding="utf-8")
        write_selection(self.path, "cloud")
        leftovers = [item.name for item in self.base.iterdir() if item.name != "config.json"]
        self.assertEqual(leftovers, [])

    def test_writing_is_idempotent(self) -> None:
        self.path.write_text(json.dumps(config_for()), encoding="utf-8")
        write_selection(self.path, "local")
        write_selection(self.path, "local")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["chat"]["model"], "local")

    def test_an_unknown_model_is_rejected_before_any_write(self) -> None:
        original = json.dumps(config_for())
        self.path.write_text(original, encoding="utf-8")

        with self.assertRaises(ValueError):
            write_selection(self.path, "gpt-9")

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_a_missing_config_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            write_selection(self.path, "local")


class ModelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "viewer").mkdir()
        (self.base / "viewer" / "index.html").write_text(
            "<h1>A.S.H.R.A</h1>", encoding="utf-8"
        )
        self.config_path = self.base / "config.json"
        self.config_path.write_text(json.dumps(config_for()), encoding="utf-8")
        self.vault = MemoryVault({"ashra/cloud": "cloud-secret"})
        self.db = Database(self.base / "data" / "test.db")
        self.app = create_app(
            self.base, database=self.db, vault=self.vault
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_get_models_reports_the_selection_and_both_options(self) -> None:
        response = self.client.get("/models")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["selected"], "local")
        self.assertEqual([item["id"] for item in payload["models"]], ["local", "cloud"])
        self.assertNotIn("api_key", json.dumps(payload))

    def test_status_carries_the_picker_state(self) -> None:
        payload = self.client.get("/status").json()
        self.assertEqual(payload["chat_model"], "local")
        cloud = next(item for item in payload["models"] if item["id"] == "cloud")
        self.assertTrue(cloud["available"])
        self.assertNotIn("api_key", json.dumps(payload))

    def test_selecting_cloud_persists_the_choice_and_audits_it(self) -> None:
        response = self.client.post("/models/select", json={"model": "cloud"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selected"], "cloud")

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["chat"]["model"], "cloud")
        self.assertEqual(saved["cloud"]["credential_ref"], "ashra/cloud")

        events = self.db.list_audit(limit=10)
        self.assertTrue(
            any(
                event["event"] == "model"
                and event["action"] == "select"
                and event.get("detail", {}).get("model") == "cloud"
                for event in events
            )
        )

    def test_selecting_an_unavailable_model_is_refused(self) -> None:
        self.vault.delete("ashra/cloud")
        response = self.client.post("/models/select", json={"model": "cloud"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "model_unavailable")
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["chat"]["model"], "local")

    def test_an_unknown_model_is_a_bad_request(self) -> None:
        response = self.client.post("/models/select", json={"model": "gpt-9"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_model")

    def test_selecting_local_works_even_with_cloud_unconfigured(self) -> None:
        raw = config_for(cloud=False)
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")

        response = self.client.post("/models/select", json={"model": "local"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["selected"], "local")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
