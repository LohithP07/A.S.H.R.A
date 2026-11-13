from __future__ import annotations

import sys
import unittest

from ashra.credentials import (
    MemoryVault,
    WindowsCredentialVault,
    get_vault,
    redact_text,
    resolve,
    validate_ref,
)
from ashra.errors import CredentialError


class ReferenceTests(unittest.TestCase):
    def test_accepts_names_from_the_prd(self) -> None:
        for ref in (
            "ashra/gmail",
            "ashra/calendar",
            "ashra/telegram",
            "ashra/cloud",
            "custom.ns/model-key",
        ):
            self.assertEqual(validate_ref(ref), ref)

    def test_rejects_values_and_traversal(self) -> None:
        for ref in ("", "gmail", "ashra/", "ashra/gmail/extra", "../secrets", "ashra/a b"):
            with self.assertRaises(ValueError, msg=ref):
                validate_ref(ref)

    def test_strips_surrounding_space(self) -> None:
        self.assertEqual(validate_ref("  ashra/gmail  "), "ashra/gmail")


class MemoryVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vault = MemoryVault()

    def test_round_trip(self) -> None:
        self.vault.set("ashra/gmail", "app-password-value")
        self.assertEqual(self.vault.get("ashra/gmail"), "app-password-value")
        self.assertTrue(self.vault.delete("ashra/gmail"))
        self.assertIsNone(self.vault.get("ashra/gmail"))
        self.assertFalse(self.vault.delete("ashra/gmail"))

    def test_refuses_empty_values(self) -> None:
        with self.assertRaises(ValueError):
            self.vault.set("ashra/gmail", "   ")

    def test_is_not_persistent(self) -> None:
        self.assertFalse(self.vault.available())

    def test_repr_lists_names_not_values(self) -> None:
        self.vault.set("ashra/gmail", "super-secret")
        self.assertIn("ashra/gmail", repr(self.vault))
        self.assertNotIn("super-secret", repr(self.vault))

    def test_redact_masks_named_refs(self) -> None:
        self.vault.set("ashra/gmail", "imap-secret")
        self.vault.set("ashra/telegram", "bot-secret")
        text = "failed with imap-secret and bot-secret"
        self.assertEqual(
            self.vault.redact(text, ["ashra/gmail"]),
            "failed with [redacted] and bot-secret",
        )
        self.assertEqual(
            self.vault.redact(text),
            "failed with [redacted] and [redacted]",
        )

    def test_resolve_explains_by_name_only(self) -> None:
        with self.assertRaises(CredentialError) as raised:
            resolve(self.vault, "ashra/calendar")
        self.assertIn("ashra/calendar", str(raised.exception))


class RedactionTests(unittest.TestCase):
    def test_longest_value_wins(self) -> None:
        self.assertEqual(
            redact_text("token=abcdef and abc", ["abc", "abcdef"]),
            "token=[redacted] and [redacted]",
        )

    def test_empty_values_are_ignored(self) -> None:
        self.assertEqual(redact_text("nothing here", ["", None]), "nothing here")


class FactoryTests(unittest.TestCase):
    def test_memory_vault_can_be_forced(self) -> None:
        self.assertIsInstance(get_vault("memory"), MemoryVault)

    def test_unknown_vault_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            get_vault("keychain")

    def test_windows_vault_matches_the_platform(self) -> None:
        if sys.platform == "win32":
            vault = WindowsCredentialVault()
            self.assertTrue(vault.available())
            self.assertEqual(repr(vault), "<WindowsCredentialVault>")
        else:
            with self.assertRaises(CredentialError):
                WindowsCredentialVault()


if __name__ == "__main__":
    unittest.main()
