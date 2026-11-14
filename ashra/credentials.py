"""Credential references and the Windows Credential Manager boundary.

PRD section 6.3: secrets live in Windows Credential Manager and the database,
logs, and configuration hold a *reference* only.  A reference looks like
``ashra/gmail``; it is a name, never a value.

``MemoryVault`` exists for tests and for machines without a credential store.
It is deliberately not a fallback for production: values disappear with the
process, which makes the limitation visible instead of quietly storing secrets
in a file.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from .errors import CredentialError

REF_PATTERN = r"^[a-z][a-z0-9_.-]{0,31}/[a-z0-9][a-z0-9_.-]{0,63}$"
REF_RE = re.compile(REF_PATTERN)
REDACTED = "[redacted]"

# Windows Credential Manager constants (wincred.h).
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


def validate_ref(ref: str) -> str:
    """Return a normalised credential reference or raise ``ValueError``.

    A malformed reference is a configuration error, not a secret leak, so the
    message may echo the input.
    """
    text = str(ref or "").strip()
    if not REF_RE.match(text):
        raise ValueError(
            f"Invalid credential reference '{ref}'. "
            "Use a name such as 'ashra/gmail'."
        )
    return text


@runtime_checkable
class CredentialVault(Protocol):
    """The narrow surface the rest of A.S.H.R.A is allowed to use."""

    def available(self) -> bool:
        """True when this vault can persist values beyond the process."""

    def get(self, ref: str) -> str | None:
        """Return the value for ``ref``, or ``None`` when it is absent."""

    def set(self, ref: str, value: str) -> None:
        """Store a value under ``ref``."""

    def delete(self, ref: str) -> bool:
        """Remove ``ref``.  Returns False when it was already absent."""

    def redact(self, text: str, refs: Iterable[str] = ()) -> str:
        """Mask any of the named values that appear in ``text``."""


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """Replace every non-empty secret in ``text`` with ``[redacted]``."""
    body = str(text or "")
    for value in sorted(
        {str(item) for item in secrets if item and str(item)},
        key=len,
        reverse=True,
    ):
        body = body.replace(value, REDACTED)
    return body


def resolve(vault: CredentialVault, ref: str) -> str:
    """Return a credential value or explain, by name only, what is missing."""
    reference = validate_ref(ref)
    value = vault.get(reference)
    if not value:
        raise CredentialError(
            f"Credential '{reference}' is not in the vault. "
            "Add it with the setup command; do not put it in config.json."
        )
    return value


class MemoryVault:
    """A process-local vault for tests and non-Windows development."""

    name = "memory"

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._values: dict[str, str] = {}
        self._lock = threading.RLock()
        for ref, value in (initial or {}).items():
            self.set(ref, value)

    def available(self) -> bool:
        return False

    def get(self, ref: str) -> str | None:
        with self._lock:
            return self._values.get(validate_ref(ref))

    def set(self, ref: str, value: str) -> None:
        reference = validate_ref(ref)
        secret = str(value or "").strip()
        if not secret:
            raise ValueError(f"Refusing to store an empty credential for '{reference}'.")
        with self._lock:
            self._values[reference] = secret

    def delete(self, ref: str) -> bool:
        with self._lock:
            return self._values.pop(validate_ref(ref), None) is not None

    def redact(self, text: str, refs: Iterable[str] = ()) -> str:
        with self._lock:
            if refs:
                secrets = [self._values.get(validate_ref(ref), "") for ref in refs]
            else:
                secrets = list(self._values.values())
        return redact_text(text, secrets)

    def __repr__(self) -> str:
        with self._lock:
            return f"<MemoryVault refs={sorted(self._values)}>"


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _CREDENTIALW(ctypes.Structure):
    # Mirror of wincred.h CREDENTIALW.  Plain ctypes types keep this module
    # importable on any platform.
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class WindowsCredentialVault:
    """Windows Credential Manager, addressed by reference name.

    Values are written as generic credentials under the reference itself
    (``ashra/gmail``), so the existing Credential Manager UI shows a name the
    owner recognises and never a value in a repository file.
    """

    name = "windows"

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise CredentialError(
                "Windows Credential Manager is only available on Windows."
            )
        self._api = ctypes.WinDLL("advapi32", use_last_error=True)
        self._api.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._api.CredReadW.restype = ctypes.c_int
        self._api.CredWriteW.argtypes = [
            ctypes.POINTER(_CREDENTIALW),
            ctypes.c_uint32,
        ]
        self._api.CredWriteW.restype = ctypes.c_int
        self._api.CredDeleteW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self._api.CredDeleteW.restype = ctypes.c_int
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None

    def available(self) -> bool:
        return True

    def get(self, ref: str) -> str | None:
        reference = validate_ref(ref)
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._api.CredReadW(
            reference,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            code = ctypes.get_last_error()
            if code == _ERROR_NOT_FOUND:
                return None
            raise CredentialError(
                f"Could not read credential '{reference}' (error {code})."
            )
        try:
            credential = pointer.contents
            size = int(credential.CredentialBlobSize)
            if size <= 0:
                return ""
            raw = ctypes.string_at(credential.CredentialBlob, size)
            return raw.decode("utf-16-le")
        finally:
            self._api.CredFree(pointer)

    def set(self, ref: str, value: str) -> None:
        reference = validate_ref(ref)
        secret = str(value or "").strip()
        if not secret:
            raise ValueError(f"Refusing to store an empty credential for '{reference}'.")

        blob = secret.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = reference
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.c_void_p)
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = reference.split("/", 1)[-1]

        if not self._api.CredWriteW(ctypes.byref(credential), 0):
            code = ctypes.get_last_error()
            raise CredentialError(
                f"Could not write credential '{reference}' (error {code})."
            )

    def delete(self, ref: str) -> bool:
        reference = validate_ref(ref)
        if self._api.CredDeleteW(reference, _CRED_TYPE_GENERIC, 0):
            return True
        if ctypes.get_last_error() == _ERROR_NOT_FOUND:
            return False
        raise CredentialError(
            f"Could not delete credential '{reference}' "
            f"(error {ctypes.get_last_error()})."
        )

    def redact(self, text: str, refs: Iterable[str] = ()) -> str:
        # Only named references can be masked here; enumerating every Windows
        # credential would be both slow and none of this module's business.
        secrets = []
        for ref in refs:
            value = self.get(ref)
            if value:
                secrets.append(value)
        return redact_text(text, secrets)

    def __repr__(self) -> str:
        return "<WindowsCredentialVault>"


def get_vault(prefer: str = "auto") -> CredentialVault:
    """Return the vault for this machine.

    ``auto`` uses Windows Credential Manager on Windows and the in-memory
    vault elsewhere (CI).  ``memory`` and ``windows`` force a choice.
    """
    choice = str(prefer or "auto").strip().lower()
    if choice == "memory":
        return MemoryVault()
    if choice in {"windows", "auto"}:
        if sys.platform == "win32":
            return WindowsCredentialVault()
        if choice == "windows":
            raise CredentialError("Windows Credential Manager is not available here.")
        return MemoryVault()
    raise ValueError(f"Unknown vault '{prefer}'. Use auto, windows, or memory.")


def main(argv: list[str] | None = None) -> int:
    """Setup command: ``python -m ashra.credentials <action> [ref]``.

    Values are read from a hidden prompt, never from the command line, so a
    secret does not land in shell history or in a process listing.
    """
    import argparse
    import getpass

    parser = argparse.ArgumentParser(
        prog="python -m ashra.credentials",
        description="Manage A.S.H.R.A credentials in Windows Credential Manager.",
    )
    parser.add_argument("action", choices=("set", "check", "delete"))
    parser.add_argument("ref", help="Reference name, for example ashra/cloud")
    args = parser.parse_args(argv)

    vault = get_vault("auto")
    if not vault.available():
        print("This vault is not persistent; values will disappear with the process.")
        return 2

    reference = args.ref
    try:
        reference = validate_ref(reference)
    except ValueError as exc:
        print(str(exc))
        return 2
    if args.action == "check":
        print("present" if vault.get(reference) else "missing")
        return 0

    if args.action == "delete":
        print("deleted" if vault.delete(reference) else "absent")
        return 0

    value = getpass.getpass(f"Value for {reference}: ").strip()
    if not value:
        print("Refusing to store an empty value.")
        return 2
    vault.set(reference, value)
    print(f"stored {reference}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
