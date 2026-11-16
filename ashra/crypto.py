"""Encryption at rest for cached private content.

PRD sections 5.4 and 6.7 require the mail cache to be encrypted at rest: a
stolen laptop must not expose message bodies.  On Windows the natural primitive
is DPAPI (``CryptProtectData``), which binds the ciphertext to the current
Windows user without a key to manage, store, back up, or leak.

Two rules keep this honest:

* the cipher declares whether it can actually encrypt, so ``/status`` can say
  "the cache is not encrypted on this machine" instead of pretending;
* the ciphertext carries a one-byte scheme marker, so a database moved between
  machines fails to decrypt visibly rather than returning nonsense.

Nothing here touches the credential vault.  Credentials are *references*; this
module only protects cached private content.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Protocol, runtime_checkable

DPAPI_MARKER = b"dpapi1\0"
PLAIN_MARKER = b"plain1\0"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


@runtime_checkable
class Cipher(Protocol):
    """The narrow surface the mailbox is allowed to use."""

    name: str

    def available(self) -> bool:
        """True when content is genuinely encrypted at rest."""

    def protect(self, data: bytes) -> bytes:
        """Return ciphertext (with a scheme marker) for ``data``."""

    def unprotect(self, data: bytes) -> bytes:
        """Return plaintext for ciphertext produced by this cipher family."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class DpapiCipher:
    """Windows DPAPI, scoped to the current user."""

    name = "dpapi"

    def available(self) -> bool:
        return sys.platform == "win32"

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _crypt32():
        return ctypes.windll.crypt32  # type: ignore[attr-defined]

    @staticmethod
    def _kernel32():
        return ctypes.windll.kernel32  # type: ignore[attr-defined]

    def _to_blob(self, data: bytes) -> tuple[_DataBlob, ctypes.Array]:
        buffer = ctypes.create_string_buffer(data, len(data))
        blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        return blob, buffer

    def _from_blob(self, blob: _DataBlob) -> bytes:
        size = int(blob.cbData)
        value = ctypes.string_at(blob.pbData, size) if size else b""
        self._kernel32().LocalFree(blob.pbData)
        return value

    # -- protocol ------------------------------------------------------
    def protect(self, data: bytes) -> bytes:
        if not self.available():
            raise OSError("DPAPI is only available on Windows.")
        raw = bytes(data)
        if not raw:
            return DPAPI_MARKER
        source, source_buffer = self._to_blob(raw)
        target = _DataBlob()
        ok = self._crypt32().CryptProtectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(target),
        )
        del source_buffer
        if not ok:
            raise OSError(
                "CryptProtectData failed; the cache could not be encrypted."
            )
        return DPAPI_MARKER + self._from_blob(target)

    def unprotect(self, data: bytes) -> bytes:
        raw = bytes(data)
        if not raw.startswith(DPAPI_MARKER):
            raise ValueError("This content was not encrypted with DPAPI.")
        payload = raw[len(DPAPI_MARKER):]
        if not payload:
            return b""
        source, source_buffer = self._to_blob(payload)
        target = _DataBlob()
        ok = self._crypt32().CryptUnprotectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(target),
        )
        del source_buffer
        if not ok:
            raise ValueError(
                "CryptUnprotectData failed; the cache belongs to another user or machine."
            )
        return self._from_blob(target)


class PlainCipher:
    """A visible no-op for machines without DPAPI.

    It is not a quiet fallback: ``available()`` is False, so status output and
    the connectors endpoint both report that the cache is unencrypted.
    """

    name = "plain"

    def available(self) -> bool:
        return False

    def protect(self, data: bytes) -> bytes:
        return PLAIN_MARKER + bytes(data)

    def unprotect(self, data: bytes) -> bytes:
        raw = bytes(data)
        if not raw.startswith(PLAIN_MARKER):
            raise ValueError("This content was not written by the plain cipher.")
        return raw[len(PLAIN_MARKER):]


def default_cipher() -> Cipher:
    """Return DPAPI on Windows and the visible no-op everywhere else."""
    dpapi = DpapiCipher()
    return dpapi if dpapi.available() else PlainCipher()
