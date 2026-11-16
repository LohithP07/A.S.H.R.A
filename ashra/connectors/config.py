"""Configuration for read-only connectors.

Phase 2E.1 adds the first real adapter, so the ``connectors`` block from PRD
Appendix B is now a validated part of ``config.json``:

    "connectors": {
      "gmail": {
        "enabled": true,
        "credential_ref": "ashra/gmail",
        "username": "owner@example.com",
        "mode": "read",
        "folders": ["INBOX", "[Gmail]/Sent Mail"],
        "retention_days": 90
      }
    }

The block contains no secret.  ``credential_ref`` names an entry in the Windows
Credential Manager; the app password itself is resolved at call time.

Rules enforced here, not by the adapter:

* ``mode`` is ``read`` in v1.0.  Anything else is a configuration error;
* folders are a short, explicit list, because a connector must never wander;
* retention is bounded, so a typo cannot keep mail forever or purge tomorrow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..credentials import validate_ref
from ..errors import ConfigError

DEFAULT_FOLDERS = ("INBOX", "[Gmail]/Sent Mail")
DEFAULT_RETENTION_DAYS = 90
MAX_FOLDERS = 8
MAX_RETENTION_DAYS = 3650
READ_ONLY_MODE = "read"


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = _text(value).lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@dataclass(frozen=True)
class MailboxConfig:
    """Validated Gmail connector settings.  Never holds a secret."""

    enabled: bool = False
    credential_ref: str = ""
    username: str = ""
    mode: str = READ_ONLY_MODE
    folders: tuple[str, ...] = DEFAULT_FOLDERS
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_per_poll: int = 50
    classify_with_model: bool = True
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        """True when a sync has everything it needs except the vault value."""
        return bool(self.enabled and self.credential_ref and self.username)

    def as_dict(self) -> dict[str, Any]:
        """A safe description: no credential value can appear here."""
        return {
            "enabled": self.enabled,
            "credential_ref": self.credential_ref or None,
            "username": self.username or None,
            "mode": self.mode,
            "folders": list(self.folders),
            "retention_days": self.retention_days,
            "max_per_poll": self.max_per_poll,
            "classify_with_model": self.classify_with_model,
        }


def _folders(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_FOLDERS
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        raise ConfigError(
            f"Connector '{name}' has an invalid 'folders' value; use a list of mailbox names."
        )
    folders = tuple(item.strip() for item in candidates if item.strip())
    if not folders:
        raise ConfigError(f"Connector '{name}' declares no folders to read.")
    if len(folders) > MAX_FOLDERS:
        raise ConfigError(
            f"Connector '{name}' declares {len(folders)} folders; at most {MAX_FOLDERS} are allowed."
        )
    return folders


def parse_mailbox_config(raw: Any, name: str = "gmail") -> MailboxConfig:
    """Validate one connector declaration from the ``connectors`` block."""
    if raw is None:
        return MailboxConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"Connector '{name}' must be an object.")

    enabled = _bool(raw.get("enabled"), False)
    mode = _text(raw.get("mode")) or READ_ONLY_MODE
    if mode != READ_ONLY_MODE:
        raise ConfigError(
            f"Connector '{name}' declares mode '{mode}'. "
            "v1.0 connectors are read-only; use 'read'."
        )

    credential_ref = _text(raw.get("credential_ref"))
    if credential_ref:
        try:
            credential_ref = validate_ref(credential_ref)
        except ValueError as exc:
            raise ConfigError(f"Connector '{name}': {exc}") from None

    if enabled and not credential_ref:
        raise ConfigError(
            f"Connector '{name}' is enabled without a credential_ref. "
            f"Use a name such as 'ashra/{name}'."
        )

    return MailboxConfig(
        enabled=enabled,
        credential_ref=credential_ref,
        username=_text(raw.get("username") or raw.get("address")),
        mode=mode,
        folders=_folders(raw.get("folders"), name),
        retention_days=_int(
            raw.get("retention_days"), DEFAULT_RETENTION_DAYS, 1, MAX_RETENTION_DAYS
        ),
        max_per_poll=_int(raw.get("max_per_poll"), 50, 1, 200),
        classify_with_model=_bool(raw.get("classify_with_model"), True),
        extras={
            key: value
            for key, value in raw.items()
            if key
            not in {
                "enabled",
                "credential_ref",
                "username",
                "address",
                "mode",
                "folders",
                "retention_days",
                "max_per_poll",
                "classify_with_model",
            }
        },
    )


def load_mailbox_config(path: Path, name: str = "gmail") -> MailboxConfig:
    """Read the connector block from ``config.json``.

    A missing file or a missing block is not an error: the connector is simply
    not configured, and Home says so.
    """
    if not Path(path).is_file():
        return MailboxConfig()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read config.json: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object.")
    connectors = raw.get("connectors")
    if connectors is None:
        return MailboxConfig()
    if not isinstance(connectors, dict):
        raise ConfigError("The 'connectors' block must be an object.")
    return parse_mailbox_config(connectors.get(name), name)
