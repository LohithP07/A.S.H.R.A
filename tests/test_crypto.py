from __future__ import annotations

import sys
import unittest

from ashra.crypto import (
    DPAPI_MARKER,
    PLAIN_MARKER,
    DpapiCipher,
    PlainCipher,
    default_cipher,
)


class PlainCipherTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        cipher = PlainCipher()
        blob = cipher.protect(b"hello world")
        self.assertTrue(blob.startswith(PLAIN_MARKER))
        self.assertEqual(cipher.unprotect(blob), b"hello world")

    def test_it_reports_that_content_is_not_encrypted(self) -> None:
        # The no-op is visible, never a quiet fallback.
        self.assertFalse(PlainCipher().available())

    def test_foreign_marker_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PlainCipher().unprotect(b"dpapi1\0not-really")

    def test_empty_content_round_trips(self) -> None:
        cipher = PlainCipher()
        self.assertEqual(cipher.unprotect(cipher.protect(b"")), b"")


@unittest.skipUnless(sys.platform == "win32", "DPAPI is a Windows API")
class DpapiCipherTests(unittest.TestCase):
    def test_round_trip_through_windows_dpapi(self) -> None:
        cipher = DpapiCipher()
        self.assertTrue(cipher.available())
        secret = b"wholesale price list, internal only"
        blob = cipher.protect(secret)
        self.assertTrue(blob.startswith(DPAPI_MARKER))
        self.assertNotIn(b"wholesale", blob)
        self.assertEqual(cipher.unprotect(blob), secret)

    def test_empty_content_round_trips(self) -> None:
        cipher = DpapiCipher()
        self.assertEqual(cipher.unprotect(cipher.protect(b"")), b"")

    def test_unmarked_content_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DpapiCipher().unprotect(b"plain text, no marker")


class DefaultCipherTests(unittest.TestCase):
    def test_default_matches_the_platform(self) -> None:
        cipher = default_cipher()
        if sys.platform == "win32":
            self.assertEqual(cipher.name, "dpapi")
        else:
            self.assertEqual(cipher.name, "plain")
            self.assertFalse(cipher.available())


if __name__ == "__main__":
    unittest.main()
