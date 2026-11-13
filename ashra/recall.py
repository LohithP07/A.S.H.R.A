"""Deterministic answers about recent captures.

Recency questions ("what did I capture this week?") have an exact answer in
the note metadata, so they never need the model.  That keeps the answer instant
and truthful, and it still works when the provider is offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .notes import Note

CAPTURE_PREFIX = "captures/"


@dataclass(frozen=True)
class RecallResult:
    answer: str
    captures: list[dict[str, object]]
    nodes: list[int]


_RECALL_PATTERNS = (
    re.compile(
        r"\bwhat\s+(?:did|have)\s+i\s+(?:capture|remember|note|save|log|jot)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwhat\s+(?:captures|notes)\s+(?:did|have)\s+i\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:show|list|tell)\s+me\s+(?:about\s+)?(?:my\s+)?"
        r"(?:recent\s+|latest\s+|newest\s+)?(?:captures|notes)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:what|which)\s+(?:are|were)\s+my\s+"
        r"(?:recent\s+|latest\s+|newest\s+)?(?:captures|notes)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bmy\s+(?:recent\s+|latest\s+|newest\s+)?captures\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:any|anything)\s+(?:new\s+)?(?:captured|noted|remembered)\b",
        re.IGNORECASE,
    ),
)

_WINDOW_RE = re.compile(
    r"\b(today|yesterday|this\s+week|last\s+week|this\s+month|last\s+month|"
    r"recently|recent|latest|newest)\b",
    re.IGNORECASE,
)


def _local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.astimezone()
    return value.astimezone()


def _window(
    question: str,
    now: datetime,
) -> tuple[datetime | None, datetime | None, str]:
    match = _WINDOW_RE.search(question)
    key = re.sub(r"\s+", " ", match.group(1).lower()) if match else ""

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if key == "today":
        return today, today + timedelta(days=1), "today"
    if key == "yesterday":
        return today - timedelta(days=1), today, "yesterday"
    if key == "this week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7), "this week"
    if key == "last week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=7), "last week"
    if key == "this month":
        start = today.replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start, end, "this month"
    if key == "last month":
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        return start, end, "last month"
    if key in {"recently", "recent", "latest", "newest"}:
        return now - timedelta(days=7), None, "recently"

    return None, None, ""


def _spoken_date(value: datetime) -> str:
    return f"{value.strftime('%A')} {value.day} {value.strftime('%B')}"


def recall_reply(
    notes: list[Note],
    question: str,
    now: datetime | None = None,
) -> RecallResult | None:
    """Answer capture-history questions, or return None for normal questions."""
    text = " ".join(question.split())
    if not text:
        return None
    if not any(pattern.search(text) for pattern in _RECALL_PATTERNS):
        return None

    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()

    start, end, label = _window(text, current)
    entries: list[tuple[datetime, Note]] = []

    for note in notes:
        if not note.path.startswith(CAPTURE_PREFIX):
            continue
        stamp = _local(note.created) or _local(note.modified)
        if stamp is None:
            continue
        if start is not None and stamp < start:
            continue
        if end is not None and stamp >= end:
            continue
        entries.append((stamp, note))

    entries.sort(key=lambda item: item[0], reverse=True)
    captures = [
        {
            "id": note.id,
            "label": note.label,
            "title": note.title,
            "path": note.path,
            "created": stamp.isoformat(timespec="seconds"),
        }
        for stamp, note in entries
    ]
    nodes = [note.id for _stamp, note in entries]

    if not entries:
        when = f" {label}" if label else " yet"
        return RecallResult(
            answer=f"Nothing captured{when}, sir.",
            captures=[],
            nodes=[],
        )

    if len(entries) == 1:
        stamp, note = entries[0]
        when = f" {label}" if label else " in the vault"
        return RecallResult(
            answer=(
                f'One capture{when}, sir: "{note.title}", '
                f"on {_spoken_date(stamp)}."
            ),
            captures=captures,
            nodes=nodes,
        )

    shown = entries[:4]
    names = ", ".join(f'"{note.title}"' for _stamp, note in shown)
    if len(entries) > len(shown):
        names = f"{names}, and {len(entries) - len(shown)} more"
    when = f" {label}" if label else " in the vault"
    return RecallResult(
        answer=f"{len(entries)} captures{when}, sir: {names}.",
        captures=captures,
        nodes=nodes,
    )
