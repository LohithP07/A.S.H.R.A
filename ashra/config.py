"""Configuration loading for the brain.

Secrets live in the project root config.json.  The viewer server is rooted at
viewer/, so this file is never reachable by the browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

PLACEHOLDER_API_KEY = "PUT-YOUR-KEY-HERE"
DEFAULT_MODEL = "claude-opus-4-8"


@dataclass(frozen=True)
class AppConfig:
    """Validated runtime configuration."""

    api_key: str = PLACEHOLDER_API_KEY
    model: str = DEFAULT_MODEL
    provider: str = "anthropic"
    max_notes: int = 6
    max_memories: int = 6
    max_history: int = 8
    max_tokens: int = 512
    temperature: float = 0.2
    note_chars: int = 4000
    api_base: str = "https://api.anthropic.com/v1/messages"
    anthropic_version: str = "2023-06-01"

    @property
    def configured(self) -> bool:
        """True when a real key and model are present."""
        key = self.api_key.strip()
        return bool(
            key
            and key != PLACEHOLDER_API_KEY
            and not key.upper().startswith("PUT-")
            and self.model.strip()
        )


def _text(data: dict, key: str, default: str) -> str:
    value = data.get(key, default)
    return str(value).strip() if value is not None else ""


def _bounded_int(data: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(data.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _bounded_float(data: dict, key: str, default: float, low: float, high: float) -> float:
    try:
        value = float(data.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def load_config(path: Path) -> AppConfig:
    """Read and validate config.json.

    The file is re-read for each chat request so the user can paste a key or
    change models without restarting the server.
    """
    if not path.is_file():
        raise ConfigError(
            f"config.json was not found at {path}. Create it next to server.py."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read config.json: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from None

    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object.")

    provider = _text(raw, "provider", "anthropic").lower()
    default_api_base = (
        "https://integrate.api.nvidia.com/v1/chat/completions"
        if provider in {"nvidia", "openai_compatible"}
        else "https://api.anthropic.com/v1/messages"
    )

    return AppConfig(
        api_key=_text(raw, "api_key", PLACEHOLDER_API_KEY),
        model=_text(raw, "model", DEFAULT_MODEL),
        provider=provider,
        max_notes=_bounded_int(raw, "max_notes", 6, 1, 12),
        max_memories=_bounded_int(raw, "max_memories", 6, 0, 20),
        max_history=_bounded_int(raw, "max_history", 8, 2, 40),
        max_tokens=_bounded_int(raw, "max_tokens", 512, 64, 4096),
        temperature=_bounded_float(raw, "temperature", 0.2, 0.0, 1.0),
        note_chars=_bounded_int(raw, "note_chars", 4000, 400, 20000),
        api_base=_text(raw, "api_base", default_api_base),
        anthropic_version=_text(raw, "anthropic_version", "2023-06-01"),
    )
