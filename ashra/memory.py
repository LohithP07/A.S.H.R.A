"""Typed, local-first memory for A.S.H.R.A.

The notes vault is one source of knowledge, not the whole mind.  This module
stores small, atomic memories with a type, provenance, confidence, and a
lifecycle:

    proposed -> confirmed -> archived
             \\-> rejected

A memory is deliberately not a transcript.  It is a claim the assistant can
retrieve, inspect, correct, and forget.  Every record carries where it came
from, so the Brain UI can answer "why do you know this?" without guessing.

Storage is SQLite plus an optional FTS5 index.  Retrieval scores lexical
overlap together with confidence, reinforcement, recency, and pinning.  The
interface is intentionally hybrid-ready: a vector index can be added behind
``search`` without changing callers.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .notes import Note
from .retrieval import tokenize

KINDS = (
    "fact",
    "preference",
    "person",
    "project",
    "event",
    "task",
    "document",
    "procedure",
    "observation",
)
STATUSES = ("proposed", "confirmed", "rejected", "archived")
SENSITIVITIES = ("normal", "private", "sensitive")

DEFAULT_CONFIDENCE = 0.5
EXPLICIT_CONFIDENCE = 0.95
DOCUMENT_CONFIDENCE = 0.7
MAX_CONTENT_CHARS = 2000
MAX_ENTITIES = 16
MAX_PROPOSALS_PER_TEXT = 6
STALE_DAYS = 180
DECAY_DAYS = 120

# A memory must never become a credential store.  These patterns catch the
# obvious shapes; the sensitivity classifier catches the rest.
_SECRET_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_-]{16,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:password|passwd|passcode)\s*[:=]\s*\S+"
    r"|(?:api[_-]?key|secret|token)\s*[:=]\s*[A-Za-z0-9_\-]{12,}"
    r")",
    re.IGNORECASE,
)

_SENSITIVE_RE = re.compile(
    r"\b(?:password|passcode|secret|token|credit\s+card|ssn|"
    r"social\s+security|medical|diagnosis|prescription|salary|bank\s+account)\b",
    re.IGNORECASE,
)

_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][a-zA-Z0-9&.'-]+(?:\s+[A-Z][a-zA-Z0-9&.'-]+){0,3})\b"
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_TAG_RE = re.compile(r"(?<!\w)#([A-Za-z][A-Za-z0-9_-]{1,40})")

_ENTITY_STOPWORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "he", "her",
    "here", "his", "how", "i", "if", "in", "is", "it", "its", "my", "now",
    "of", "on", "or", "our", "she", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "to", "today", "tomorrow",
    "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "with", "yesterday", "you", "your", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february",
    "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december",
}

_REMEMBER_PREFIX_RE = re.compile(
    r"^(?:a\.?\s*s\.?\s*h\.?\s*r\.?\s*a[,\s]+)?(?:please\s+)?"
    r"(?:remember(?:\s+that)?|note\s+that|save(?:\s+that)?|"
    r"make\s+a\s+note(?:\s+that)?|jot\s+down(?:\s+that)?|log(?:\s+that)?)"
    r"[:\s]+",
    re.IGNORECASE,
)

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# validation and normalisation
# ---------------------------------------------------------------------------
def clean_content(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) < 3:
        raise ValueError("A memory needs at least three characters.")
    if len(text) > MAX_CONTENT_CHARS:
        raise ValueError(
            f"A memory is capped at {MAX_CONTENT_CHARS:,} characters. "
            "Split it into smaller claims."
        )
    if _SECRET_RE.search(text):
        raise ValueError("I will not store credentials or secrets in memory.")
    return text


def normalize_content(value: str) -> str:
    """A stable key for duplicate detection (case and punctuation free)."""
    return " ".join(_WORD_RE.findall(value.lower()))


def validate_kind(value: str | None) -> str:
    kind = str(value or "fact").strip().lower()
    if kind not in KINDS:
        raise ValueError(
            f"Unknown memory kind '{value}'. Use one of: {', '.join(KINDS)}."
        )
    return kind


def validate_status(value: str | None) -> str:
    status = str(value or "proposed").strip().lower()
    if status not in STATUSES:
        raise ValueError(
            f"Unknown memory status '{value}'. Use one of: {', '.join(STATUSES)}."
        )
    return status


def validate_sensitivity(value: str | None) -> str:
    sensitivity = str(value or "normal").strip().lower()
    if sensitivity not in SENSITIVITIES:
        raise ValueError(
            f"Unknown sensitivity '{value}'. Use one of: {', '.join(SENSITIVITIES)}."
        )
    return sensitivity


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE
    if math.isnan(number):
        return DEFAULT_CONFIDENCE
    return max(low, min(high, number))


def normalize_entities(values: Iterable[Any] | None) -> list[str]:
    seen: set[str] = set()
    entities: list[str] = []
    for value in values or ():
        text = " ".join(str(value or "").split()).strip(" .,;:!?—-")
        if len(text) < 2 or len(text) > 80:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        entities.append(text)
        if len(entities) >= MAX_ENTITIES:
            break
    return entities


def extract_entities(text: str) -> list[str]:
    """Pull people, projects, URLs, emails, and tags out of a claim."""
    found: list[str] = []
    for match in _URL_RE.finditer(text):
        found.append(match.group(0))
    for match in _EMAIL_RE.finditer(text):
        found.append(match.group(0))
    for match in _TAG_RE.finditer(text):
        found.append(match.group(1))
    for match in _ENTITY_RE.finditer(text):
        phrase = match.group(0).strip(" .,;:!?—-")
        words = phrase.split()
        if not words:
            continue
        if words[0].lower() in _ENTITY_STOPWORDS:
            # Keep a multi-word phrase only when the head is meaningful.
            if len(words) < 2 or words[1].lower() in _ENTITY_STOPWORDS:
                continue
            phrase = " ".join(words[1:])
        if len(phrase) < 3:
            continue
        found.append(phrase)
    return normalize_entities(found)


def infer_kind(text: str, default: str = "observation") -> str:
    lower = text.lower()
    if re.search(
        r"\b(?:i|we)\s+(?:prefer|like|love|hate|avoid|want|need|always|never)\b",
        lower,
    ):
        return "preference"
    if re.search(r"\b(?:remind\s+me|todo|to-do|need\s+to|must|follow\s+up|deadline)\b", lower):
        return "task"
    if re.search(r"\b(?:project|initiative|launch|milestone|roadmap)\b", lower):
        return "project"
    if re.search(
        r"\b(?:process|procedure|workflow|playbook|steps?\s+to|when\s+.*\s+(?:do|use))\b",
        lower,
    ):
        return "procedure"
    if re.search(
        r"\b(?:meeting|call|appointment|event|on\s+\d{4}-\d{2}-\d{2})\b",
        lower,
    ):
        return "event"
    if re.search(
        r"\b(?:my|our)\s+(?:wife|husband|partner|friend|boss|manager|"
        r"colleague|mother|father|son|daughter|team)\b",
        lower,
    ):
        return "person"
    return default


def infer_confidence(text: str, kind: str) -> float:
    if kind in {"preference", "task", "person"}:
        return 0.78
    if kind == "document":
        return DOCUMENT_CONFIDENCE
    if kind == "procedure":
        return 0.7
    if re.search(r"\b(?:definitely|always|never|is|are|was|were)\b", text.lower()):
        return 0.6
    return DEFAULT_CONFIDENCE


def infer_sensitivity(text: str) -> str:
    return "sensitive" if _SENSITIVE_RE.search(text) else "normal"


def split_memories(text: str) -> list[str]:
    cleaned = _REMEMBER_PREFIX_RE.sub("", " ".join(str(text or "").split())).strip()
    if not cleaned:
        return []
    pieces = [piece.strip(" .,;:!?—-") for piece in _SENTENCE_RE.split(cleaned)]
    pieces = [piece for piece in pieces if len(piece) >= 8]
    if not pieces:
        pieces = [cleaned]
    return pieces[:MAX_PROPOSALS_PER_TEXT]


# ---------------------------------------------------------------------------
# row helpers
# ---------------------------------------------------------------------------
def _json_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _label(content: str, limit: int = 84) -> str:
    text = " ".join(content.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut}…"


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["entities"] = _json_list(data.get("entities"))
    data["metadata"] = _json_dict(data.get("metadata"))
    data["pinned"] = bool(data.get("pinned"))
    data["label"] = _label(str(data.get("content") or ""))
    return data


def _score(row: sqlite3.Row, terms: set[str], now: float) -> float:
    if not terms:
        return 0.0
    haystack = str(row["content"]) + " " + " ".join(_json_list(row["entities"]))
    content_terms = set(tokenize(haystack))
    overlap = terms & content_terms
    if not overlap:
        return 0.0

    score = len(overlap) * 3.0
    score += float(row["confidence"]) * 2.0
    score += min(int(row["reinforcement"]), 10) * 0.12
    score += 2.0 if row["pinned"] else 0.0
    age_days = max(0.0, (now - float(row["last_seen_at"])) / 86400)
    score += max(0.0, 1.5 - age_days / 45.0)
    return round(score, 3)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class MemoryStore:
    """SQLite-backed memory with a propose/confirm lifecycle."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def add(
        self,
        content: str,
        *,
        kind: str = "fact",
        status: str = "proposed",
        source: str = "manual",
        source_ref: str | None = None,
        confidence: float | None = None,
        sensitivity: str | None = None,
        entities: Iterable[str] | None = None,
        pinned: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = clean_content(content)
        kind = validate_kind(kind)
        status = validate_status(status)
        sensitivity = validate_sensitivity(sensitivity or infer_sensitivity(text))
        confidence = _clamp(
            confidence if confidence is not None else infer_confidence(text, kind)
        )
        entity_list = normalize_entities(entities or extract_entities(text))
        normalized = normalize_content(text)
        if not normalized:
            raise ValueError("A memory needs at least one word.")

        now = time.time()
        with self.database._lock, self.database._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM memories WHERE normalized = ? AND status != 'rejected' "
                "ORDER BY CASE status WHEN 'confirmed' THEN 0 ELSE 1 END, created_at "
                "LIMIT 1",
                (normalized,),
            ).fetchone()
            if existing is not None:
                promoted = status == "confirmed" and existing["status"] in {
                    "proposed",
                    "archived",
                }
                connection.execute(
                    "UPDATE memories SET last_seen_at = ?, updated_at = ?, "
                    "reinforcement = reinforcement + 1, confidence = MAX(confidence, ?), "
                    "pinned = MAX(pinned, ?), status = CASE WHEN ? THEN 'confirmed' "
                    "ELSE status END WHERE id = ?",
                    (
                        now,
                        now,
                        confidence,
                        int(bool(pinned)),
                        int(promoted),
                        existing["id"],
                    ),
                )
                if promoted:
                    self._event(connection, existing["id"], "confirm", {"reason": "duplicate"})
                row = connection.execute(
                    "SELECT * FROM memories WHERE id = ?",
                    (existing["id"],),
                ).fetchone()
                result = _row_dict(row) if row is not None else _row_dict(existing)
                result["duplicate"] = True
                return result

            memory_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO memories
                    (id, created_at, updated_at, last_seen_at, kind, content,
                     normalized, entities, source, source_ref, confidence,
                     sensitivity, status, pinned, reinforcement, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    memory_id,
                    now,
                    now,
                    now,
                    kind,
                    text,
                    normalized,
                    json.dumps(entity_list, ensure_ascii=False),
                    str(source or "manual"),
                    source_ref,
                    confidence,
                    sensitivity,
                    status,
                    int(bool(pinned)),
                    json.dumps(metadata, ensure_ascii=False, default=str)
                    if metadata is not None
                    else None,
                ),
            )
            self._index(connection, memory_id, text)
            self._link_entities(connection, memory_id, entity_list)
            self._event(connection, memory_id, "create", {"status": status, "kind": kind})
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
        result = _row_dict(row) if row is not None else {}
        result["duplicate"] = False
        return result

    def propose(
        self,
        content: str,
        *,
        kind: str | None = None,
        source: str = "manual",
        source_ref: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        return self.add(
            content,
            kind=kind or infer_kind(content),
            status="proposed",
            source=source,
            source_ref=source_ref,
            confidence=confidence,
        )

    def remember(
        self,
        content: str,
        *,
        kind: str | None = None,
        source: str = "manual",
        source_ref: str | None = None,
        confidence: float = EXPLICIT_CONFIDENCE,
    ) -> dict[str, Any]:
        return self.add(
            content,
            kind=kind or infer_kind(content, default="fact"),
            status="confirmed",
            source=source,
            source_ref=source_ref,
            confidence=confidence,
        )

    def propose_from_text(
        self,
        text: str,
        *,
        source: str = "manual",
        source_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        proposals: list[dict[str, Any]] = []
        for piece in split_memories(text):
            proposals.append(
                self.propose(
                    piece,
                    kind=infer_kind(piece, default="fact"),
                    source=source,
                    source_ref=source_ref,
                )
            )
        return proposals

    def remember_explicit(
        self,
        text: str,
        *,
        source: str = "manual",
        source_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        memories: list[dict[str, Any]] = []
        for piece in split_memories(text):
            memories.append(
                self.remember(
                    piece,
                    kind=infer_kind(piece, default="fact"),
                    source=source,
                    source_ref=source_ref,
                )
            )
        return memories

    def ingest_notes(self, notes: Iterable[Note]) -> dict[str, int]:
        added = 0
        updated = 0
        seen = 0
        for note in notes:
            seen += 1
            content = (note.excerpt or note.text or "").strip()
            if len(content) < 8:
                continue
            source_ref = f"notes/{note.path}"
            existing = self._find_by_source_ref(source_ref, kind="document")
            metadata = {
                "title": note.title,
                "path": note.path,
                "group": note.group,
                "words": note.words,
            }
            if existing is not None:
                if existing["content"] != content:
                    self.update(
                        existing["id"],
                        content=content,
                        entities=extract_entities(f"{note.title} {content}"),
                        metadata=metadata,
                    )
                else:
                    self._touch(existing["id"])
                updated += 1
                continue
            self.add(
                content,
                kind="document",
                status="confirmed",
                source="notes",
                source_ref=source_ref,
                confidence=DOCUMENT_CONFIDENCE,
                entities=extract_entities(f"{note.title} {content}"),
                metadata=metadata,
            )
            added += 1
        return {"notes": seen, "added": added, "updated": updated}

    def confirm(self, memory_id: str) -> dict[str, Any] | None:
        return self._set_status(memory_id, "confirmed", confidence=0.85)

    def reject(self, memory_id: str) -> dict[str, Any] | None:
        return self._set_status(memory_id, "rejected")

    def archive(self, memory_id: str) -> dict[str, Any] | None:
        return self._set_status(memory_id, "archived")

    def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        kind: str | None = None,
        pinned: bool | None = None,
        confidence: float | None = None,
        sensitivity: str | None = None,
        entities: Iterable[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self.database._lock, self.database._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (str(memory_id),),
            ).fetchone()
            if row is None:
                return None

            text = clean_content(content) if content is not None else str(row["content"])
            new_kind = validate_kind(kind) if kind is not None else str(row["kind"])
            new_sensitivity = (
                validate_sensitivity(sensitivity)
                if sensitivity is not None
                else str(row["sensitivity"])
            )
            new_confidence = (
                _clamp(confidence)
                if confidence is not None
                else float(row["confidence"])
            )
            new_entities = (
                normalize_entities(entities)
                if entities is not None
                else (
                    extract_entities(text)
                    if content is not None
                    else _json_list(row["entities"])
                )
            )
            new_metadata = (
                json.dumps(metadata, ensure_ascii=False, default=str)
                if metadata is not None
                else row["metadata"]
            )
            normalized = normalize_content(text)

            connection.execute(
                """
                UPDATE memories SET updated_at = ?, content = ?, normalized = ?,
                    kind = ?, pinned = ?, confidence = ?, sensitivity = ?,
                    entities = ?, metadata = ?
                WHERE id = ?
                """,
                (
                    time.time(),
                    text,
                    normalized,
                    new_kind,
                    int(bool(pinned)) if pinned is not None else int(row["pinned"]),
                    new_confidence,
                    new_sensitivity,
                    json.dumps(new_entities, ensure_ascii=False),
                    new_metadata,
                    str(memory_id),
                ),
            )
            self._unindex(connection, str(memory_id))
            self._index(connection, str(memory_id), text)
            connection.execute(
                "DELETE FROM memory_links WHERE source = ? OR target = ?",
                (str(memory_id), str(memory_id)),
            )
            self._link_entities(connection, str(memory_id), new_entities)
            self._event(connection, str(memory_id), "update", {"kind": new_kind})
            updated = connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (str(memory_id),),
            ).fetchone()
        return _row_dict(updated) if updated is not None else None

    def forget(self, memory_id: str) -> bool:
        with self.database._lock, self.database._connect() as connection:
            row = connection.execute(
                "SELECT id FROM memories WHERE id = ?",
                (str(memory_id),),
            ).fetchone()
            if row is None:
                return False
            self._unindex(connection, str(memory_id))
            connection.execute(
                "DELETE FROM memory_links WHERE source = ? OR target = ?",
                (str(memory_id), str(memory_id)),
            )
            connection.execute(
                "DELETE FROM memory_events WHERE memory_id = ?",
                (str(memory_id),),
            )
            connection.execute("DELETE FROM memories WHERE id = ?", (str(memory_id),))
        return True

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def get(self, memory_id: str) -> dict[str, Any] | None:
        with self.database._lock, self.database._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (str(memory_id),),
            ).fetchone()
        return _row_dict(row) if row is not None else None

    def list(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        pinned: bool | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit)))
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status = ?")
            values.append(validate_status(status))
        if kind:
            clauses.append("kind = ?")
            values.append(validate_kind(kind))
        if pinned is not None:
            clauses.append("pinned = ?")
            values.append(int(bool(pinned)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database._lock, self.database._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memories {where} "
                "ORDER BY pinned DESC, created_at DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_row_dict(row) for row in rows]

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        status: str | None = "confirmed",
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        text = " ".join(str(query or "").split())
        if not text:
            return self.list(status=status, kind=kind, limit=limit)

        terms = set(tokenize(text))
        if not terms:
            return self.list(status=status, kind=kind, limit=limit)

        now = time.time()
        rows = self._candidate_rows(text, status=status, kind=kind)
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            score = _score(row, terms, now)
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], -float(item[1]["created_at"])))
        results: list[dict[str, Any]] = []
        for score, row in scored[:limit]:
            item = _row_dict(row)
            item["score"] = score
            results.append(item)
        return results

    def related(self, memory_id: str, limit: int = 12) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        with self.database._lock, self.database._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, l.weight FROM memory_links l
                JOIN memories m ON m.id = CASE
                    WHEN l.source = ? THEN l.target ELSE l.source END
                WHERE l.source = ? OR l.target = ?
                ORDER BY l.weight DESC, m.created_at DESC
                LIMIT ?
                """,
                (str(memory_id), str(memory_id), str(memory_id), limit),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = _row_dict(row)
            item["weight"] = float(row["weight"])
            results.append(item)
        return results

    def graph(self, *, status: str | None = None, limit: int = 500) -> dict[str, Any]:
        rows = self.list(status=status, limit=limit)
        by_id = {item["id"]: item for item in rows}
        if not by_id:
            return {"nodes": [], "links": []}

        with self.database._lock, self.database._connect() as connection:
            link_rows = connection.execute(
                "SELECT source, target, kind, weight FROM memory_links"
            ).fetchall()

        links: list[dict[str, Any]] = []
        degree: dict[str, int] = {memory_id: 0 for memory_id in by_id}
        seen: set[tuple[str, str, str]] = set()
        for row in link_rows:
            source = str(row["source"])
            target = str(row["target"])
            if source not in by_id or target not in by_id:
                continue
            key = (source, target, str(row["kind"]))
            if key in seen:
                continue
            seen.add(key)
            degree[source] += 1
            degree[target] += 1
            links.append(
                {
                    "source": source,
                    "target": target,
                    "kind": str(row["kind"]),
                    "weight": float(row["weight"]),
                }
            )

        nodes = [
            {
                "id": item["id"],
                "label": item["label"],
                "kind": item["kind"],
                "status": item["status"],
                "confidence": item["confidence"],
                "pinned": item["pinned"],
                "degree": degree.get(item["id"], 0),
                "created_at": item["created_at"],
                "source": item["source"],
            }
            for item in rows
        ]
        return {"nodes": nodes, "links": links}

    def timeline(
        self,
        *,
        status: str | None = None,
        limit: int = 120,
    ) -> list[dict[str, Any]]:
        rows = self.list(status=status, limit=limit)
        rows.sort(key=lambda item: float(item["created_at"]), reverse=True)
        for item in rows:
            item["created_iso"] = datetime.fromtimestamp(
                float(item["created_at"]), tz=timezone.utc
            ).isoformat()
        return rows

    def stats(self) -> dict[str, Any]:
        now = time.time()
        with self.database._lock, self.database._connect() as connection:
            total = connection.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
            by_status = {
                str(row["status"]): int(row["n"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM memories GROUP BY status"
                ).fetchall()
            }
            by_kind = {
                str(row["kind"]): int(row["n"])
                for row in connection.execute(
                    "SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind"
                ).fetchall()
            }
            by_source = {
                str(row["source"]): int(row["n"])
                for row in connection.execute(
                    "SELECT source, COUNT(*) AS n FROM memories GROUP BY source"
                ).fetchall()
            }
            pinned = connection.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE pinned = 1"
            ).fetchone()["n"]
            sensitive = connection.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE sensitivity != 'normal'"
            ).fetchone()["n"]
            link_count = connection.execute(
                "SELECT COUNT(*) AS n FROM memory_links"
            ).fetchone()["n"]
            stale = connection.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE status = 'confirmed' "
                "AND last_seen_at < ?",
                (now - STALE_DAYS * 86400,),
            ).fetchone()["n"]
            low_confidence = connection.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE status = 'confirmed' "
                "AND confidence < 0.5"
            ).fetchone()["n"]
            duplicate_groups = connection.execute(
                "SELECT COUNT(*) AS n FROM (SELECT normalized FROM memories "
                "WHERE status = 'confirmed' GROUP BY normalized HAVING COUNT(*) > 1)"
            ).fetchone()["n"]
            orphaned = connection.execute(
                "SELECT COUNT(*) AS n FROM memories m WHERE m.status = 'confirmed' "
                "AND NOT EXISTS (SELECT 1 FROM memory_links l "
                "WHERE l.source = m.id OR l.target = m.id)"
            ).fetchone()["n"]

        return {
            "total": int(total),
            "proposed": int(by_status.get("proposed", 0)),
            "confirmed": int(by_status.get("confirmed", 0)),
            "rejected": int(by_status.get("rejected", 0)),
            "archived": int(by_status.get("archived", 0)),
            "by_status": by_status,
            "by_kind": by_kind,
            "by_source": by_source,
            "pinned": int(pinned),
            "sensitive": int(sensitive),
            "links": int(link_count),
            "health": {
                "stale": int(stale),
                "orphaned": int(orphaned),
                "low_confidence": int(low_confidence),
                "duplicate_groups": int(duplicate_groups),
            },
        }

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    def consolidate(self) -> dict[str, int]:
        """Merge exact duplicates and gently decay memories nobody has seen."""
        merged = 0
        archived = 0
        now = time.time()
        with self.database._lock, self.database._connect() as connection:
            groups = connection.execute(
                "SELECT normalized, GROUP_CONCAT(id) AS ids FROM memories "
                "WHERE status = 'confirmed' GROUP BY normalized HAVING COUNT(*) > 1"
            ).fetchall()
            for group in groups:
                ids = [part for part in str(group["ids"]).split(",") if part]
                if len(ids) < 2:
                    continue
                rows = connection.execute(
                    f"SELECT * FROM memories WHERE id IN ({','.join('?' for _ in ids)}) "
                    "ORDER BY pinned DESC, confidence DESC, created_at",
                    tuple(ids),
                ).fetchall()
                if not rows:
                    continue
                keeper = rows[0]
                for row in rows[1:]:
                    connection.execute(
                        "UPDATE memories SET status = 'archived', updated_at = ? "
                        "WHERE id = ?",
                        (now, row["id"]),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO memory_links "
                        "(source, target, kind, weight) VALUES (?, ?, 'duplicate', 1.0)",
                        (keeper["id"], row["id"]),
                    )
                    self._event(
                        connection,
                        row["id"],
                        "archive",
                        {"reason": "duplicate", "keeper": keeper["id"]},
                    )
                    archived += 1
                connection.execute(
                    "UPDATE memories SET reinforcement = reinforcement + ?, "
                    "last_seen_at = ?, updated_at = ? WHERE id = ?",
                    (len(rows) - 1, now, now, keeper["id"]),
                )
                merged += 1

            cutoff = now - DECAY_DAYS * 86400
            cursor = connection.execute(
                "UPDATE memories SET confidence = MAX(0.2, confidence - 0.05), "
                "updated_at = ? WHERE status = 'confirmed' AND pinned = 0 "
                "AND last_seen_at < ?",
                (now, cutoff),
            )
            decayed = int(cursor.rowcount or 0)

        return {"merged_groups": merged, "archived": archived, "decayed": decayed}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _find_by_source_ref(
        self,
        source_ref: str,
        *,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        with self.database._lock, self.database._connect() as connection:
            if kind:
                row = connection.execute(
                    "SELECT * FROM memories WHERE source_ref = ? AND kind = ? LIMIT 1",
                    (source_ref, kind),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM memories WHERE source_ref = ? LIMIT 1",
                    (source_ref,),
                ).fetchone()
        return _row_dict(row) if row is not None else None

    def _touch(self, memory_id: str) -> None:
        with self.database._lock, self.database._connect() as connection:
            connection.execute(
                "UPDATE memories SET last_seen_at = ? WHERE id = ?",
                (time.time(), str(memory_id)),
            )

    def _set_status(
        self,
        memory_id: str,
        status: str,
        *,
        confidence: float | None = None,
    ) -> dict[str, Any] | None:
        status = validate_status(status)
        with self.database._lock, self.database._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (str(memory_id),),
            ).fetchone()
            if row is None:
                return None
            new_confidence = (
                max(float(row["confidence"]), _clamp(confidence))
                if confidence is not None
                else float(row["confidence"])
            )
            connection.execute(
                "UPDATE memories SET status = ?, updated_at = ?, confidence = ? "
                "WHERE id = ?",
                (status, time.time(), new_confidence, str(memory_id)),
            )
            self._event(connection, str(memory_id), status, {})
            updated = connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (str(memory_id),),
            ).fetchone()
        return _row_dict(updated) if updated is not None else None

    def _candidate_rows(
        self,
        query: str,
        *,
        status: str | None,
        kind: str | None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("m.status = ?")
            values.append(validate_status(status))
        else:
            clauses.append("m.status != 'rejected'")
        if kind:
            clauses.append("m.kind = ?")
            values.append(validate_kind(kind))
        where = f"AND {' AND '.join(clauses)}" if clauses else ""

        if self.database.fts_enabled:
            tokens = [token for token in tokenize(query)][:12]
            if tokens:
                match = " OR ".join(f'"{token}"' for token in tokens)
                try:
                    with self.database._lock, self.database._connect() as connection:
                        rows = connection.execute(
                            "SELECT m.* FROM memories_fts f "
                            "JOIN memories m ON m.id = f.memory_id "
                            f"WHERE memories_fts MATCH ? {where} LIMIT 500",
                            (match, *values),
                        ).fetchall()
                    if rows:
                        return rows
                except sqlite3.OperationalError:
                    pass

        with self.database._lock, self.database._connect() as connection:
            rows = connection.execute(
                f"SELECT m.* FROM memories m WHERE 1 = 1 {where} "
                "ORDER BY m.pinned DESC, m.created_at DESC LIMIT 500",
                tuple(values),
            ).fetchall()
        return rows

    def _index(self, connection: sqlite3.Connection, memory_id: str, content: str) -> None:
        if not self.database.fts_enabled:
            return
        try:
            connection.execute(
                "INSERT INTO memories_fts (content, memory_id) VALUES (?, ?)",
                (content, memory_id),
            )
        except sqlite3.OperationalError:
            pass

    def _unindex(self, connection: sqlite3.Connection, memory_id: str) -> None:
        if not self.database.fts_enabled:
            return
        try:
            connection.execute(
                "DELETE FROM memories_fts WHERE memory_id = ?",
                (memory_id,),
            )
        except sqlite3.OperationalError:
            pass

    def _link_entities(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
        entities: list[str],
    ) -> None:
        wanted = {entity.lower() for entity in entities}
        if not wanted:
            return
        rows = connection.execute(
            "SELECT id, entities FROM memories WHERE id != ? "
            "AND status != 'rejected' LIMIT 500",
            (memory_id,),
        ).fetchall()
        scored: list[tuple[int, str]] = []
        for row in rows:
            other = {str(entity).lower() for entity in _json_list(row["entities"])}
            shared = wanted & other
            if shared:
                scored.append((len(shared), str(row["id"])))
        scored.sort(key=lambda item: (-item[0], item[1]))
        for weight, other_id in scored[:6]:
            source, target = (
                (memory_id, other_id) if memory_id < other_id else (other_id, memory_id)
            )
            connection.execute(
                "INSERT OR REPLACE INTO memory_links "
                "(source, target, kind, weight) VALUES (?, ?, 'shared_entity', ?)",
                (source, target, float(weight)),
            )

    def _event(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
        event: str,
        detail: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO memory_events (memory_id, created_at, event, detail) "
            "VALUES (?, ?, ?, ?)",
            (
                memory_id,
                time.time(),
                str(event),
                json.dumps(detail, ensure_ascii=False, default=str),
            ),
        )
