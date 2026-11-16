"""The answer-model catalog and the owner's selection.

A.S.H.R.A has two answer models, and they are not interchangeable:

``local``
    Ollama on loopback.  Every class of content may use it, because nothing
    leaves the machine.
``cloud``
    An OpenAI-compatible provider.  The privacy router still decides per
    request: public and internal content may go out, private content stays
    local, and secret content is refused.

The selection lives in ``config.json`` as ``chat.model`` and is read per
request, so a click in the Home panel takes effect on the next question
without a restart.  This module owns two things only: the catalog (what exists
and what is usable) and the writer (how a choice is persisted).  It never
returns a credential value; availability is a boolean plus a reason.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CHAT_MODELS, DEFAULT_CHAT_MODEL, AppConfig
from .errors import ConfigError

LOCAL = "local"
CLOUD = "cloud"

LOCAL_NOTE = "Private · nothing leaves this machine"
CLOUD_NOTE = "Public and internal only · prompts leave this machine"


@dataclass(frozen=True)
class ModelOption:
    """One selectable answer model, with no secret in it."""

    id: str
    label: str
    provider: str
    model: str
    lane: str
    available: bool
    detail: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "model": self.model,
            "lane": self.lane,
            "available": self.available,
            "detail": self.detail,
            "note": self.note,
        }


def _local_configured(config: AppConfig) -> bool:
    # ``for_local`` invents the default model name for a cloud-primary config,
    # so ask whether a local lane was actually declared.  The lane needs a
    # model, never a key.
    if config.local_model.strip():
        return True
    return config.provider in {"ollama", "local"} and bool(config.model.strip())


def _cloud_snapshot(config: AppConfig, vault: object | None) -> AppConfig:
    try:
        return config.for_cloud(vault)
    except Exception:  # pragma: no cover - defensive; for_cloud does not raise
        return config


def catalog(config: AppConfig, vault: object | None = None) -> list[ModelOption]:
    """Both models, in picker order, with availability and the reason."""
    local_snapshot = config.for_local()
    local_model = config.local_model or (
        local_snapshot.model if config.provider in {"ollama", "local"} else ""
    )
    local_ready = _local_configured(config)
    local = ModelOption(
        id=LOCAL,
        label="Local",
        provider=local_snapshot.provider,
        model=local_model,
        lane="local",
        available=local_ready,
        detail=LOCAL_NOTE if local_ready else "No local model is declared",
        note=LOCAL_NOTE,
    )

    cloud_snapshot = _cloud_snapshot(config, vault)
    cloud_model = cloud_snapshot.model
    cloud_ready = bool(config.cloud_configured and cloud_snapshot.configured)
    if not config.cloud_configured:
        cloud_detail = "No cloud model is configured"
    elif not cloud_ready:
        cloud_detail = "The cloud credential is missing from the vault"
    else:
        cloud_detail = CLOUD_NOTE
    cloud = ModelOption(
        id=CLOUD,
        label="Cloud",
        provider=cloud_snapshot.provider,
        model=cloud_model,
        lane="cloud",
        available=cloud_ready,
        detail=cloud_detail,
        note=CLOUD_NOTE,
    )
    return [local, cloud]


def find(options: list[ModelOption], choice: str) -> ModelOption | None:
    wanted = str(choice or "").strip().lower()
    for option in options:
        if option.id == wanted:
            return option
    return None


def selected(config: AppConfig) -> str:
    """The configured choice, clamped to something the picker can show."""
    choice = str(config.chat_model or "").strip().lower()
    return choice if choice in CHAT_MODELS else DEFAULT_CHAT_MODEL


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
def write_selection(path: Path, choice: str) -> str:
    """Persist ``chat.model`` while preserving every other key.

    The file is read, the one field is replaced, and the result is written to a
    sibling temporary file before ``os.replace`` swaps it in.  A crash leaves
    either the old file or the new one, never a half-written config.  Values
    are never logged; this module does not read any secret.
    """
    wanted = str(choice or "").strip().lower()
    if wanted not in CHAT_MODELS:
        raise ValueError(
            "Choose a model from: " + ", ".join(CHAT_MODELS) + "."
        )

    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config.json was not found at {path}.") from None
    except OSError as exc:
        raise ConfigError(f"Could not read config.json: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object.")

    chat = raw.get("chat")
    if not isinstance(chat, dict):
        chat = {}
    raw["chat"] = {**chat, "model": wanted}

    text = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=".config-", suffix=".json"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ConfigError(f"Could not write config.json: {exc}") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return wanted
