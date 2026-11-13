from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build
from ashra.remember import append, forget, remember, rename


class RememberTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.notes = self.base / "notes"
        self.notes.mkdir()
        self.viewer = self.base / "viewer"
        self.viewer.mkdir()

        (self.notes / "pricing-strategy.md").write_text(
            "# Pricing Strategy\n\nOur pricing strategy is value based.\n",
            encoding="utf-8",
        )

        self.build_patch = patch.multiple(
            build,
            BASE_DIR=self.base,
            NOTES_DIR=self.notes,
            VIEWER_DIR=self.viewer,
            OUTPUT_FILE=self.viewer / "graph-data.js",
        )
        self.build_patch.start()
        self.addCleanup(self.build_patch.stop)

    def read(self, path: str) -> str:
        return (self.notes / path).read_text(encoding="utf-8")

    def test_remember_creates_a_capture_and_a_related_link(self) -> None:
        result = remember(
            self.base,
            "Remember that pricing strategy should stay value based.",
        )

        self.assertTrue((self.notes / result.path).is_file())
        self.assertIn("pricing strategy", result.title.lower())
        self.assertIn("Related: [[", self.read("pricing-strategy.md"))
        self.assertIn(result.title, self.read("pricing-strategy.md"))
        self.assertTrue((self.viewer / "graph-data.js").is_file())

    def test_append_rename_and_forget(self) -> None:
        created = remember(self.base, "Remember that the spring blend is ready.")
        appended = append(
            self.base,
            "Also tell the wholesale team.",
            path=created.path,
        )

        self.assertEqual(appended.title, created.title)
        self.assertIn("Also tell the wholesale team.", self.read(created.path))

        renamed = rename(self.base, "Spring Blend Launch", path=created.path)

        self.assertFalse((self.notes / created.path).exists())
        self.assertTrue((self.notes / renamed.path).is_file())
        self.assertIn("# Spring Blend Launch", self.read(renamed.path))

        forget(self.base, path=renamed.path)
        self.assertFalse((self.notes / renamed.path).exists())

    def test_forget_rejects_paths_outside_captures(self) -> None:
        with self.assertRaises(ValueError):
            forget(self.base, path="../pricing-strategy.md")

    def test_remember_rejects_short_captures(self) -> None:
        with self.assertRaises(ValueError):
            remember(self.base, "hi")


if __name__ == "__main__":
    unittest.main()
