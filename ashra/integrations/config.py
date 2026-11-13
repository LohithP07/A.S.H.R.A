"""Configuration for external MCP integrations.

Integrations are declared in the same ``config.json`` as the model provider:

    "integrations": [
      {
        "name": "filesystem",
        "transport": "stdio",
        "command": ["python", "-m", "ashra.mcp_server", "--root", "notes"],
        "permission": "read",
        "timeout": 20
      },
      {
        "name": "calendar",
        "transport": "http",
        "url": "http://127.0.0.1:8765/mcp",
        "permission": "read"
      }
    ]

Phase 1D is deliberately read-only.  A write-capable integration has to wait for
the draft/act layer, where every side effect gets an approval and an allowlist.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigError, ToolInputError
from ..tools import Permission, parse_permission

TRANSPORTS = {"stdio", "http", "streamable_http"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_TIMEOUT = 20.0


@dataclass(frozen=True)
class IntegrationConfig:
    """One external MCP server A.S.H.R.A may use."""

    name: str
    transport: str = "stdio"
    command: tuple[str, ...] = ()
    url: str = ""
    permission: Permission = Permission.READ
    enabled: bool = True
    timeout: float = DEFAULT_TIMEOUT
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None

    @property
    def label(self) -> str:
        """A short human-readable description for status output."""
        if self.transport == "stdio":
            return " ".join(self.command)
        return self.url

    @property
    def prefix(self) -> str:
        """The stable tool-name prefix for this integration."""
        return re.sub(r"[^a-z0-9]+", "_", self.name).strip("_") or "integration"


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _command(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            parts = shlex.split(value, posix=False)
        except ValueError as exc:
            raise ConfigError(f"Integration '{name}' has an invalid command: {exc}") from None
        # posix=False keeps quotes around Windows paths; strip them here.
        parts = [part.strip().strip('"').strip("'") for part in parts]
    elif isinstance(value, list):
        parts = [str(item) for item in value]
    else:
        raise ConfigError(
            f"Integration '{name}' needs a 'command' list for stdio transport."
        )

    parts = [part for part in parts if part.strip()]
    if not parts:
        raise ConfigError(
            f"Integration '{name}' needs a 'command' list for stdio transport."
        )
    return tuple(parts)


def _timeout(value: Any, name: str) -> float:
    try:
        seconds = float(value if value is not None else DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        raise ConfigError(f"Integration '{name}' has an invalid timeout.") from None
    if not 1.0 <= seconds <= 120.0:
        raise ConfigError(f"Integration '{name}' timeout must be between 1 and 120 seconds.")
    return seconds


def _env(value: Any, name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"Integration '{name}' env must be a JSON object.")
    env: dict[str, str] = {}
    for key, item in value.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        env[key_text] = str(item)
    return env


def parse_integration(raw: Any, *, index: int = 0) -> IntegrationConfig:
    """Validate one integration entry from config.json."""
    if not isinstance(raw, dict):
        raise ConfigError(f"Integration #{index + 1} must be a JSON object.")

    name = _text(raw.get("name")).lower()
    if not name:
        raise ConfigError(f"Integration #{index + 1} needs a name.")
    if not NAME_RE.match(name):
        raise ConfigError(
            f"Integration name '{name}' must use lowercase letters, digits, "
            "underscores, or hyphens (max 64 characters)."
        )

    transport = _text(raw.get("transport", "stdio")).lower() or "stdio"
    if transport not in TRANSPORTS:
        raise ConfigError(
            f"Integration '{name}' has unknown transport '{transport}'. "
            "Use 'stdio' or 'http'."
        )
    if transport == "streamable_http":
        transport = "http"

    permission = raw.get("permission", "read")
    try:
        permission = parse_permission(permission)
    except ToolInputError as exc:
        raise ConfigError(f"Integration '{name}': {exc}") from None
    if permission is not Permission.READ:
        raise ConfigError(
            f"Integration '{name}' requests '{permission.value}' permission, "
            "but Phase 1D integrations are read-only. Use 'read'."
        )

    enabled_raw = raw.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        raise ConfigError(f"Integration '{name}' enabled must be true or false.")
    enabled = enabled_raw
    timeout = _timeout(raw.get("timeout"), name)

    if transport == "stdio":
        return IntegrationConfig(
            name=name,
            transport=transport,
            command=_command(raw.get("command"), name),
            permission=permission,
            enabled=enabled,
            timeout=timeout,
            env=_env(raw.get("env"), name),
            cwd=_text(raw.get("cwd")) or None,
        )

    url = _text(raw.get("url"))
    if not url:
        raise ConfigError(f"Integration '{name}' needs a 'url' for http transport.")
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"Integration '{name}' url must start with http:// or https://.")
    return IntegrationConfig(
        name=name,
        transport=transport,
        url=url,
        permission=permission,
        enabled=enabled,
        timeout=timeout,
    )


def load_integrations(path: Path) -> list[IntegrationConfig]:
    """Read the optional ``integrations`` array from config.json.

    A missing file or a file without integrations means "none", not an error:
    the assistant still works entirely offline.
    """
    path = Path(path)
    if not path.is_file():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read config.json: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from None

    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object.")

    entries = raw.get("integrations")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ConfigError("'integrations' in config.json must be a list.")

    integrations: list[IntegrationConfig] = []
    seen: set[str] = set()
    prefixes: set[str] = set()
    for index, entry in enumerate(entries):
        config = parse_integration(entry, index=index)
        if config.name in seen:
            raise ConfigError(f"Integration '{config.name}' is declared twice.")
        if config.prefix in prefixes:
            raise ConfigError(
                f"Integration '{config.name}' collides with another integration's "
                f"tool prefix '{config.prefix}'."
            )
        seen.add(config.name)
        prefixes.add(config.prefix)
        integrations.append(config)
    return integrations
