"""The connector contract: declared capabilities, polling, and health.

PRD section 7.1 describes one adapter per external system.  The contract is
deliberately narrow so a new provider cannot quietly acquire new authority:

    id            "gmail" | "gcal" | "files" | "browser"
    capabilities  { read, draft, write, delete }   declared, enforced
    scopes        folders, domains, calendars, roots
    auth          credential_ref -> Windows Credential Manager
    poll()        incremental fetch, idempotent by external id
    normalise()   external record -> Source + candidate task/memory
    health()      last success, last error, backlog, latency
    redact()      strip secrets and sensitive fields before logging

No connector in v1.0 may write, delete, or send.  The base class enforces the
declaration at the point of use, so an adapter that tries to exceed its
capability fails visibly instead of relying on a reviewer to notice.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from .. import sensitivity as privacy
from ..credentials import validate_ref
from ..errors import CapabilityDenied, ConnectorError, RateLimited

CONNECTOR_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def validate_connector_id(value: str) -> str:
    text = str(value or "").strip()
    if not CONNECTOR_ID_RE.match(text):
        raise ValueError(
            f"Invalid connector id '{value}'. Use 2-32 lowercase characters, "
            "digits, or underscores, starting with a letter."
        )
    return text


class Capability(str, Enum):
    READ = "read"
    DRAFT = "draft"
    WRITE = "write"
    DELETE = "delete"

    @property
    def label(self) -> str:
        return self.value


def parse_capability(value: Capability | str) -> Capability:
    if isinstance(value, Capability):
        return value
    try:
        return Capability(str(value or "").strip().lower())
    except ValueError:
        allowed = ", ".join(item.value for item in Capability)
        raise ValueError(
            f"Unknown connector capability '{value}'. Use one of: {allowed}."
        ) from None


@dataclass(frozen=True)
class Capabilities:
    """What a connector declares it may do.  Read-only is the default."""

    read: bool = True
    draft: bool = False
    write: bool = False
    delete: bool = False

    def __post_init__(self) -> None:
        if self.delete and not self.write:
            raise ValueError("A connector cannot declare delete without write.")

    def allows(self, capability: Capability | str) -> bool:
        return bool(getattr(self, parse_capability(capability).value))

    def require(self, capability: Capability | str, connector_id: str = "") -> None:
        wanted = parse_capability(capability)
        if not self.allows(wanted):
            name = f"'{connector_id}'" if connector_id else "This connector"
            raise CapabilityDenied(
                f"{name} did not declare the '{wanted.value}' capability."
            )

    def as_dict(self) -> dict[str, bool]:
        return {
            "read": self.read,
            "draft": self.draft,
            "write": self.write,
            "delete": self.delete,
        }

    def as_list(self) -> list[str]:
        return [item.value for item in Capability if self.allows(item)]

    @classmethod
    def parse(cls, value: Any) -> "Capabilities":
        if value is None:
            return cls()
        if isinstance(value, Capabilities):
            return value
        if isinstance(value, str):
            return cls.from_names([value])
        if isinstance(value, Mapping):
            known = {"read", "draft", "write", "delete"}
            unknown = set(str(key) for key in value) - known
            if unknown:
                raise ValueError(
                    "Unknown connector capabilities: " + ", ".join(sorted(unknown))
                )
            return cls(**{key: bool(value.get(key, False)) for key in known})
        if isinstance(value, Iterable):
            return cls.from_names(list(value))
        raise ValueError("Connector capabilities must be a name, list, or object.")

    @classmethod
    def from_names(cls, names: Iterable[str]) -> "Capabilities":
        granted = {parse_capability(name).value for name in names}
        return cls(**{name: name in granted for name in ("read", "draft", "write", "delete")})


@dataclass
class ConnectorHealth:
    """Last-known health.  Never contains message bodies or secrets."""

    connector_id: str
    ok: bool = False
    status: str = "unknown"
    last_success_at: float | None = None
    last_error_at: float | None = None
    last_error: str | None = None
    items_last_poll: int = 0
    backlog: int | None = None
    latency_ms: float | None = None
    checked_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "last_success_at": self.last_success_at,
            "last_error_at": self.last_error_at,
            "last_error": self.last_error,
            "items_last_poll": self.items_last_poll,
            "backlog": self.backlog,
            "latency_ms": self.latency_ms,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True)
class Record:
    """One raw item from an external system, before normalisation.

    ``payload`` is untrusted data.  It is never treated as an instruction, and
    nothing in it may name a recipient, a tool, or a scope.
    """

    external_id: str
    kind: str
    subject: str = ""
    occurred_at: float | None = None
    thread_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    sensitivity: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A proposed task or memory.  Proposals need confirmation downstream."""

    kind: str
    title: str
    detail: str = ""
    due_at: float | None = None
    person: str | None = None
    confidence: float = 0.5
    sensitivity: str | None = None
    source_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "due_at": self.due_at,
            "person": self.person,
            "confidence": self.confidence,
            "sensitivity": self.sensitivity,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class Source:
    """A normalised record: the durable pointer to where a claim came from."""

    connector_id: str
    kind: str
    external_id: str | None = None
    thread_id: str | None = None
    subject: str = ""
    occurred_at: float | None = None
    local_path: str | None = None
    sensitivity: str = "private"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sensitivity", privacy.parse(self.sensitivity))

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "kind": self.kind,
            "external_id": self.external_id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "occurred_at": self.occurred_at,
            "local_path": self.local_path,
            "sensitivity": self.sensitivity,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class NormalisedRecord:
    source: Source
    candidates: tuple[Candidate, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.as_dict(),
            "candidates": [item.as_dict() for item in self.candidates],
        }


@dataclass
class PollResult:
    """One incremental poll.  ``cursor`` is opaque to everyone but the adapter."""

    items: list[Record] = field(default_factory=list)
    cursor: str | None = None
    fetched: int = 0
    skipped: int = 0
    exhausted: bool = False

    def __post_init__(self) -> None:
        if not self.fetched:
            self.fetched = len(self.items)


class Connector(ABC):
    """Base class for every external adapter.

    Subclasses implement :meth:`poll` and :meth:`normalise`.  Everything else —
    capability enforcement, rate limiting, health, redaction — is shared so an
    adapter cannot forget it.
    """

    def __init__(
        self,
        *,
        connector_id: str,
        label: str = "",
        capabilities: Capabilities | Mapping[str, Any] | Iterable[str] | str | None = None,
        scopes: Mapping[str, Any] | None = None,
        credential_ref: str | None = None,
        min_interval_seconds: float = 0.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.id = validate_connector_id(connector_id)
        self.label = str(label or self.id)
        self.capabilities = Capabilities.parse(capabilities)
        self.scopes = dict(scopes or {})
        self.credential_ref = validate_ref(credential_ref) if credential_ref else None
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._clock = clock or time.monotonic
        self._last_poll_at: float | None = None
        self._health = ConnectorHealth(connector_id=self.id)

    # ------------------------------------------------------------------
    # adapter surface
    # ------------------------------------------------------------------
    @abstractmethod
    def poll(self, cursor: str | None = None) -> PollResult:
        """Fetch items newer than ``cursor``.  Must be idempotent by external id."""

    @abstractmethod
    def normalise(self, record: Record) -> NormalisedRecord | None:
        """Map one external record to a Source plus candidate proposals."""

    def redact(self, value: Any) -> Any:
        """Strip secrets before a value is logged or returned.  Adapters override."""
        return value

    # ------------------------------------------------------------------
    # shared behaviour
    # ------------------------------------------------------------------
    def guard(self, capability: Capability | str) -> None:
        self.capabilities.require(capability, self.id)

    def run(self, cursor: str | None = None, *, force: bool = False) -> PollResult:
        """Rate-limited, health-recording wrapper around :meth:`poll`."""
        self.guard(Capability.READ)
        started_at = self._clock()
        if not force and self.min_interval_seconds and self._last_poll_at is not None:
            elapsed = started_at - self._last_poll_at
            if elapsed < self.min_interval_seconds:
                wait = self.min_interval_seconds - elapsed
                raise RateLimited(
                    f"Connector '{self.id}' was polled too recently; retry in {wait:.1f}s."
                )

        try:
            result = self.poll(cursor)
        except Exception as exc:  # noqa: BLE001 - health must record any failure
            self.record_error(exc)
            raise
        latency_ms = max(0.0, (self._clock() - started_at) * 1000.0)
        self._last_poll_at = started_at
        self.record_success(result.fetched, latency_ms)
        return result

    def record_success(self, fetched: int = 0, latency_ms: float | None = None) -> None:
        now = time.time()
        self._health.ok = True
        self._health.status = "ready"
        self._health.last_success_at = now
        self._health.checked_at = now
        self._health.items_last_poll = max(0, int(fetched))
        if latency_ms is not None:
            self._health.latency_ms = round(float(latency_ms), 1)

    def record_error(self, exc: BaseException) -> None:
        now = time.time()
        message = str(exc).strip() or type(exc).__name__
        self._health.ok = False
        self._health.status = "error"
        self._health.last_error_at = now
        self._health.checked_at = now
        self._health.last_error = str(self.redact(message))[:300]

    def health(self) -> ConnectorHealth:
        return replace(self._health)

    def status(self) -> dict[str, Any]:
        """A safe description: declarations plus health, never credential values."""
        return {
            "id": self.id,
            "label": self.label,
            "capabilities": self.capabilities.as_dict(),
            "capability_list": self.capabilities.as_list(),
            "scopes": dict(self.scopes),
            "credential_ref": self.credential_ref,
            "health": self.health().as_dict(),
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id}>"


def require_capability(
    connector: Connector,
    capability: Capability | str,
) -> None:
    """Module-level guard used by the registry."""
    if not isinstance(connector, Connector):
        raise ConnectorError("Expected a Connector instance.")
    connector.guard(capability)
