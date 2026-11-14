"""Configuration loading for the brain.

Secrets live in the project root config.json.  The viewer server is rooted at
viewer/, so this file is never reachable by the browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from .errors import ConfigError

PLACEHOLDER_API_KEY = "PUT-YOUR-KEY-HERE"
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_LOCAL_BASE = "http://127.0.0.1:11434"
DEFAULT_LOCAL_MODEL = "qwen3.5:2b-q4_K_M"
DEFAULT_PUBLIC_CLASSES = ("public", "internal")


def _is_real_key(value: str) -> bool:
    key = str(value or "").strip()
    return bool(
        key
        and key != PLACEHOLDER_API_KEY
        and not key.upper().startswith("PUT-")
    )


@dataclass(frozen=True)
class AppConfig:
    """Validated runtime configuration.

    ``provider``/``model``/``api_base`` describe the *primary* lane.  The cloud
    block is the optional second lane: a provider, a model, and a credential
    reference whose value lives in the vault.  The local block describes the
    Ollama lane, which never carries a key.
    """

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
    credential_ref: str = ""
    # Local lane (Ollama). No key, no network egress.
    local_model: str = ""
    local_api_base: str = DEFAULT_LOCAL_BASE
    local_num_ctx: int = 8192
    local_think: bool = False
    local_format: str = ""
    local_timeout: int = 30
    # Cloud lane.
    cloud_provider: str = ""
    cloud_model: str = ""
    cloud_api_base: str = ""
    cloud_credential_ref: str = ""
    # Privacy policy.
    privacy_default_lane: str = "local"
    privacy_allow_cloud: tuple[str, ...] = DEFAULT_PUBLIC_CLASSES
    privacy_refuse: tuple[str, ...] = ("secret",)

    @property
    def configured(self) -> bool:
        """True when the primary lane can answer.

        The local lane needs a model but never a key.  Remote providers need a
        real key, or a credential reference that the vault can resolve.
        """
        if not self.model.strip() and not self.local_model.strip():
            return False
        if self.provider in {"ollama", "local"}:
            return bool((self.local_model or self.model).strip())
        return _is_real_key(self.api_key) or bool(self.credential_ref)

    @property
    def cloud_configured(self) -> bool:
        """True when a cloud lane is declared with a model and a credential."""
        provider = self.cloud_provider or (
            self.provider if self.provider not in {"ollama", "local"} else ""
        )
        model = self.cloud_model or (
            self.model if self.provider not in {"ollama", "local"} else ""
        )
        if not provider or not model.strip():
            return False
        if provider in {"ollama", "local"}:
            return True
        return bool(self.cloud_credential_ref or _is_real_key(self.api_key))

    def for_local(self) -> "AppConfig":
        """Return the configuration the local lane should use."""
        model = self.local_model or (
            self.model if self.provider in {"ollama", "local"} else DEFAULT_LOCAL_MODEL
        )
        return replace(
            self,
            provider="ollama",
            model=model,
            api_base=self.local_api_base or DEFAULT_LOCAL_BASE,
            api_key="",
        )

    def for_cloud(self, vault: object | None = None) -> "AppConfig":
        """Return the configuration the cloud lane should use.

        The credential is read from the vault when a reference is declared.
        Nothing is invented: an unresolved reference stays unconfigured so the
        caller can degrade instead of silently reaching the network.
        """
        provider = self.cloud_provider or (
            self.provider if self.provider not in {"ollama", "local"} else ""
        )
        model = self.cloud_model or (
            self.model if self.provider not in {"ollama", "local"} else ""
        )
        api_base = self.cloud_api_base or (
            self.api_base if self.provider not in {"ollama", "local"} else ""
        )
        key = self.api_key
        ref = self.cloud_credential_ref or self.credential_ref
        if ref:
            # A declared reference means the vault is the only source.  Falling
            # back to a stale key in config would defeat the point of the
            # reference, so an unresolved one leaves the lane unconfigured.
            stored = None
            if vault is not None:
                try:
                    stored = vault.get(ref)  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover - defensive
                    stored = None
            key = stored or ""
        return replace(
            self,
            provider=provider or "openai_compatible",
            model=model,
            api_base=api_base,
            api_key=key,
        )


def _text(data: dict, key: str, default: str) -> str:
    value = data.get(key, default)
    return str(value).strip() if value is not None else ""


def _section(data: dict, key: str) -> dict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _classes(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return default
    names = tuple(
        str(item).strip().lower() for item in value if str(item or "").strip()
    )
    return names or default


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


def _bounded_bool(data: dict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


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
        DEFAULT_LOCAL_BASE
        if provider in {"ollama", "local"}
        else (
            "https://integrate.api.nvidia.com/v1/chat/completions"
            if provider in {"nvidia", "openai_compatible"}
            else "https://api.anthropic.com/v1/messages"
        )
    )
    cloud = _section(raw, "cloud")
    privacy = _section(raw, "privacy")
    local = _section(raw, "local")

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
        credential_ref=_text(raw, "credential_ref", ""),
        local_model=_text(local, "model", _text(raw, "local_model", "")),
        local_api_base=_text(local, "api_base", DEFAULT_LOCAL_BASE),
        local_num_ctx=_bounded_int(local, "num_ctx", 8192, 512, 131072),
        local_think=_bounded_bool(local, "think", False),
        local_format=_text(local, "format", ""),
        local_timeout=_bounded_int(local, "timeout_s", 30, 5, 300),
        cloud_provider=_text(cloud, "provider", ""),
        cloud_model=_text(cloud, "model", ""),
        cloud_api_base=_text(cloud, "api_base", ""),
        cloud_credential_ref=_text(cloud, "credential_ref", ""),
        privacy_default_lane=_text(privacy, "default_lane", "local"),
        privacy_allow_cloud=_classes(
            privacy.get("allow_cloud_for"), DEFAULT_PUBLIC_CLASSES
        ),
        privacy_refuse=_classes(privacy.get("refuse"), ("secret",)),
    )
