"""A deterministic connector for fixtures, tests, and onboarding previews.

Phase 2E.0 must be reviewable without Gmail, a calendar, or credentials.  The
replay connector feeds recorded items through the exact same contract a real
adapter will implement, so the registry, deduplication, proposal mapping, and
health paths are exercised before any network code exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .. import sensitivity as privacy
from .base import Candidate, Connector, NormalisedRecord, PollResult, Record, Source

_CANDIDATE_FIELDS = {
    "kind",
    "title",
    "detail",
    "due_at",
    "person",
    "confidence",
    "sensitivity",
    "source_ref",
}


class ReplayConnector(Connector):
    """Replay a fixed list of records in pages.

    The cursor is the index of the next record, which makes a second poll over
    the same list a genuine idempotency test: Sources update in place.
    """

    def __init__(
        self,
        records: Iterable[Record | Mapping[str, Any]] = (),
        *,
        connector_id: str = "replay",
        label: str = "Replay fixture",
        page_size: int = 50,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("capabilities", {"read": True})
        kwargs.setdefault("scopes", {"fixture": True})
        super().__init__(connector_id=connector_id, label=label, **kwargs)
        self.records = [self._as_record(item) for item in records]
        self.page_size = max(1, int(page_size))

    @staticmethod
    def _as_record(item: Record | Mapping[str, Any]) -> Record:
        if isinstance(item, Record):
            return item
        if not isinstance(item, Mapping):
            raise TypeError("Replay records must be Record instances or mappings.")
        return Record(
            external_id=str(item["external_id"]),
            kind=str(item.get("kind") or "message"),
            subject=str(item.get("subject") or ""),
            occurred_at=item.get("occurred_at"),
            thread_id=item.get("thread_id"),
            payload=dict(item.get("payload") or {}),
            sensitivity=item.get("sensitivity"),
        )

    def poll(self, cursor: str | None = None) -> PollResult:
        try:
            start = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            start = 0
        page = self.records[start : start + self.page_size]
        next_index = start + len(page)
        exhausted = next_index >= len(self.records)
        return PollResult(
            items=list(page),
            cursor=None if exhausted else str(next_index),
            fetched=len(page),
            exhausted=exhausted,
        )

    def normalise(self, record: Record) -> NormalisedRecord:
        body = str(record.payload.get("body") or "")
        declared = record.sensitivity or privacy.classify(
            f"{record.subject} {body}",
            default="internal",
        )
        source = Source(
            connector_id=self.id,
            kind=record.kind,
            external_id=record.external_id,
            thread_id=record.thread_id,
            subject=record.subject,
            occurred_at=record.occurred_at,
            local_path=record.payload.get("local_path"),
            sensitivity=declared,
            metadata=dict(record.payload.get("metadata") or {}),
        )
        candidates = tuple(
            Candidate(
                **{
                    key: value
                    for key, value in dict(raw).items()
                    if key in _CANDIDATE_FIELDS
                }
            )
            for raw in record.payload.get("candidates") or ()
        )
        return NormalisedRecord(source=source, candidates=candidates)
