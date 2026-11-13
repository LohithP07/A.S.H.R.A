"""Connector registry: declare once, poll safely, inspect without secrets.

The registry is the only place that knows the set of configured external
systems.  It enforces two rules:

* a connector must exist and declare a capability before it is used;
* a poll produces Sources (deduplicated by external id) and *proposals*, never
  tasks or memories directly.

``ingest`` is the reusable path Phase 2E.1 and 2E.2 build on.  It is also the
proof that polling is idempotent: a second pass over the same fixture updates
existing Sources instead of duplicating them.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any

from .. import sensitivity as privacy
from ..credentials import CredentialVault
from ..db import Database
from ..errors import ConnectorError, ConnectorNotFound
from .base import (
    Capability,
    Connector,
    NormalisedRecord,
    Source,
    require_capability,
)


@dataclass
class IngestResult:
    """What one poll produced, in terms the Desk can later display."""

    connector_id: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    cursor: str | None = None
    exhausted: bool = False
    sources: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "cursor": self.cursor,
            "exhausted": self.exhausted,
            "sources": self.sources,
            "candidates": self.candidates,
        }


class ConnectorRegistry:
    """A small, ordered registry of adapters."""

    def __init__(
        self,
        connectors: Iterable[Connector] | None = None,
        *,
        database: Database | None = None,
    ) -> None:
        self._connectors: dict[str, Connector] = {}
        self._database = database
        for connector in connectors or ():
            self.register(connector)

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------
    def register(self, connector: Connector) -> Connector:
        if not isinstance(connector, Connector):
            raise ConnectorError("A connector must subclass ashra.connectors.Connector.")
        if connector.id in self._connectors:
            raise ValueError(f"Connector '{connector.id}' is already registered.")
        self._connectors[connector.id] = connector
        self._persist(connector)
        return connector

    def unregister(self, connector_id: str) -> bool:
        return self._connectors.pop(str(connector_id), None) is not None

    def find(self, connector_id: str) -> Connector | None:
        return self._connectors.get(str(connector_id or "").strip())

    def get(self, connector_id: str) -> Connector:
        connector = self.find(connector_id)
        if connector is None:
            known = ", ".join(self.ids()) or "none configured"
            raise ConnectorNotFound(
                f"Connector '{connector_id}' is not registered ({known})."
            )
        return connector

    def require(self, connector_id: str, capability: Capability | str) -> Connector:
        connector = self.get(connector_id)
        require_capability(connector, capability)
        return connector

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def list(self) -> list[Connector]:
        return [self._connectors[key] for key in sorted(self._connectors)]

    def ids(self) -> list[str]:
        return sorted(self._connectors)

    def status(self) -> list[dict[str, Any]]:
        return [connector.status() for connector in self.list()]

    def health(self) -> list[dict[str, Any]]:
        return [
            {"id": connector.id, **connector.health().as_dict()}
            for connector in self.list()
        ]

    def credentials(self, vault: CredentialVault) -> list[dict[str, Any]]:
        """Report which references resolve — never the values themselves."""
        rows: list[dict[str, Any]] = []
        for connector in self.list():
            ref = connector.credential_ref
            ready = True
            if ref:
                try:
                    ready = bool(vault.get(ref))
                except Exception:  # noqa: BLE001 - a broken vault is not fatal here
                    ready = False
            rows.append(
                {
                    "id": connector.id,
                    "credential_ref": ref,
                    "ready": ready,
                    "vault": getattr(vault, "name", "unknown"),
                }
            )
        return rows

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------
    def ingest(
        self,
        connector_id: str,
        cursor: str | None = None,
        *,
        force: bool = False,
    ) -> IngestResult:
        """Poll one connector and persist its Sources and candidate proposals."""
        connector = self.require(connector_id, Capability.READ)
        polled = connector.run(cursor, force=force)
        result = IngestResult(
            connector_id=connector.id,
            fetched=polled.fetched,
            cursor=polled.cursor,
            exhausted=polled.exhausted,
        )

        for record in polled.items:
            try:
                normalised = connector.normalise(record)
            except Exception as exc:  # noqa: BLE001 - one bad item must not stop a poll
                result.skipped += 1
                connector.record_error(exc)
                continue
            if normalised is None:
                result.skipped += 1
                continue

            source = self._effective_source(record, normalised)
            source_id, created = self._store(source)
            stored = dict(source.as_dict())
            stored["id"] = source_id or None
            result.sources.append(stored)
            if created:
                result.created += 1
            else:
                result.updated += 1

            for candidate in normalised.candidates:
                proposed = replace(
                    candidate,
                    source_ref=source_id,
                    sensitivity=privacy.highest(
                        candidate.sensitivity or source.sensitivity,
                        source.sensitivity,
                    ),
                )
                result.candidates.append(proposed.as_dict())

        self._persist(connector)
        return result

    @staticmethod
    def _effective_source(
        record: Any,
        normalised: NormalisedRecord,
    ) -> Source:
        declared = record.sensitivity
        effective = privacy.highest(
            normalised.source.sensitivity,
            declared if declared else None,
        )
        if effective == normalised.source.sensitivity:
            return normalised.source
        return replace(normalised.source, sensitivity=effective)

    def _store(self, source: Source) -> tuple[str, bool]:
        if self._database is None:
            return "", True
        return self._database.upsert_source(
            connector_id=source.connector_id,
            kind=source.kind,
            external_id=source.external_id,
            thread_id=source.thread_id,
            subject=source.subject,
            occurred_at=source.occurred_at,
            local_path=source.local_path,
            sensitivity=source.sensitivity,
            metadata=source.metadata or None,
        )

    def _persist(self, connector: Connector) -> None:
        if self._database is None:
            return
        health = connector.health()
        self._database.upsert_connector_state(
            connector.id,
            kind=connector.id,
            credential_ref=connector.credential_ref,
            scopes=connector.scopes,
            capabilities=connector.capabilities.as_dict(),
            status=health.status,
            error=health.last_error,
            last_sync_at=health.last_success_at,
        )

    def __len__(self) -> int:
        return len(self._connectors)

    def __contains__(self, connector_id: object) -> bool:
        return str(connector_id) in self._connectors

    def __iter__(self) -> Iterator[Connector]:
        return iter(self.list())
