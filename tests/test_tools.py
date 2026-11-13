from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashra.errors import (
    ApprovalRequired,
    PermissionDenied,
    ToolInputError,
    ToolNotFound,
)
from ashra.remember import RememberResult
from ashra.tools import (
    Permission,
    ScopedFilesystem,
    build_default_registry,
    parse_permission,
)


def fake_capture() -> RememberResult:
    return RememberResult(
        title="Test Capture",
        path="captures/test-capture.md",
        node=1,
        related=None,
        graph={"nodes": [], "links": []},
        reply="Noted, sir.",
    )


class ScopedFilesystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.notes = self.base / "notes"
        (self.notes / "sub").mkdir(parents=True)
        (self.notes / "alpha.md").write_text(
            "# Alpha\n\nOur pricing is value based. " + ("detail " * 40) + "\n",
            encoding="utf-8",
        )
        (self.notes / "sub" / "beta.md").write_text(
            "# Beta\n\nA second note.\n",
            encoding="utf-8",
        )
        (self.notes / "config.json").write_text("{}", encoding="utf-8")
        (self.notes / ".env").write_text("SECRET=1", encoding="utf-8")
        (self.notes / "script.exe").write_text("nope", encoding="utf-8")
        self.fs = ScopedFilesystem(self.notes)

    def test_list_directory_hides_blocked_and_unknown_files(self) -> None:
        payload = self.fs.list_directory()
        names = {entry["name"] for entry in payload["entries"]}
        self.assertIn("alpha.md", names)
        self.assertIn("sub", names)
        self.assertNotIn("config.json", names)
        self.assertNotIn(".env", names)
        self.assertNotIn("script.exe", names)

    def test_read_text(self) -> None:
        payload = self.fs.read_text("alpha.md")
        self.assertIn("pricing is value based", payload["content"])
        self.assertFalse(payload["truncated"])

    def test_read_truncates(self) -> None:
        payload = self.fs.read_text("alpha.md", max_chars=100)
        self.assertLessEqual(len(payload["content"]), 100)
        self.assertTrue(payload["truncated"])

    def test_read_rejects_traversal(self) -> None:
        with self.assertRaises(ToolInputError):
            self.fs.read_text("../config.json")

    def test_read_rejects_absolute_paths(self) -> None:
        with self.assertRaises(ToolInputError):
            self.fs.read_text(str(self.notes / "alpha.md"))

    def test_read_rejects_blocked_names(self) -> None:
        with self.assertRaises(ToolInputError):
            self.fs.read_text("config.json")
        with self.assertRaises(ToolInputError):
            self.fs.read_text(".env")

    def test_read_rejects_unknown_suffixes(self) -> None:
        with self.assertRaises(ToolInputError):
            self.fs.read_text("script.exe")

    def test_read_rejects_missing_files(self) -> None:
        with self.assertRaises(ToolInputError):
            self.fs.read_text("missing.md")

    def test_search_finds_matching_lines(self) -> None:
        payload = self.fs.search("pricing")
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["path"], "alpha.md")

    def test_search_requires_a_query(self) -> None:
        with self.assertRaises(ToolInputError):
            self.fs.search("x")


class ToolRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "notes").mkdir()
        (self.base / "notes" / "alpha.md").write_text(
            "# Alpha\n\nOur pricing is value based.\n",
            encoding="utf-8",
        )
        self.records: list[dict] = []
        self.registry = build_default_registry(self.base, audit=self.records.append)

    def test_parse_permission(self) -> None:
        self.assertEqual(parse_permission("read"), Permission.READ)
        self.assertEqual(parse_permission(Permission.ACT), Permission.ACT)
        with self.assertRaises(ToolInputError):
            parse_permission("owner")

    def test_read_tools_are_visible_to_read_callers(self) -> None:
        names = {tool["name"] for tool in self.registry.list(Permission.READ)}
        self.assertIn("fs_read", names)
        self.assertNotIn("vault_remember", names)

    def test_write_tools_need_act_permission(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.registry.invoke(
                "vault_remember",
                {"text": "hello there"},
                granted=Permission.READ,
            )

    def test_write_tools_need_approval(self) -> None:
        with self.assertRaises(ApprovalRequired):
            self.registry.invoke(
                "vault_remember",
                {"text": "hello there"},
                granted=Permission.ACT,
            )

    def test_approved_write_calls_the_handler(self) -> None:
        with patch("ashra.tools.remember_capture", return_value=fake_capture()) as mocked:
            result = self.registry.invoke(
                "vault_remember",
                {"text": "hello there"},
                granted=Permission.ACT,
                approved=True,
            )

        self.assertEqual(result["title"], "Test Capture")
        mocked.assert_called_once()

    def test_unknown_tools_are_rejected(self) -> None:
        with self.assertRaises(ToolNotFound):
            self.registry.invoke("nope")

    def test_arguments_are_validated(self) -> None:
        with self.assertRaises(ToolInputError):
            self.registry.invoke("fs_read", {})
        with self.assertRaises(ToolInputError):
            self.registry.invoke("fs_read", {"relative_path": "alpha.md", "extra": 1})
        with self.assertRaises(ToolInputError):
            self.registry.invoke("fs_read", {"relative_path": 5})

    def test_successful_calls_are_audited(self) -> None:
        self.registry.invoke("fs_read", {"relative_path": "alpha.md"})
        self.assertEqual(self.records[-1]["tool"], "fs_read")
        self.assertEqual(self.records[-1]["status"], "ok")

    def test_failed_handlers_are_audited(self) -> None:
        with self.assertRaises(ToolInputError):
            self.registry.invoke("fs_read", {"relative_path": "missing.md"})
        self.assertEqual(self.records[-1]["tool"], "fs_read")
        self.assertEqual(self.records[-1]["status"], "error")


if __name__ == "__main__":
    unittest.main()
