"""Keyword retrieval over the notes.

The first implementation deliberately uses transparent keyword overlap rather
than embeddings.  It is fast, dependency-free, and easy to replace later with a
vector index without changing the /chat contract.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from .notes import Note

STOPWORDS = {
    "a", "about", "after", "again", "against", "all", "also", "am", "an", "and",
    "any", "are", "as", "at", "be", "because", "been", "before", "being", "but",
    "by", "can", "could", "did", "do", "does", "doing", "done", "for", "from",
    "had", "has", "have", "having", "he", "her", "here", "him", "his", "how",
    "i", "if", "in", "into", "is", "it", "its", "just", "me", "more", "most",
    "much", "my", "no", "not", "now", "of", "on", "once", "only", "or", "other",
    "our", "out", "over", "own", "please", "same", "she", "should", "so", "some",
    "such", "than", "thank", "thanks", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "through", "to", "too", "under",
    "up", "us", "very", "was", "we", "were", "what", "when", "where", "which",
    "while", "who", "why", "will", "with", "would", "yes", "you", "your",
}

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'’_-]*", re.IGNORECASE)
RECENCY_RE = re.compile(
    r"\b(?:recent|recently|latest|newest|today|yesterday|this\s+week|"
    r"last\s+week|this\s+month)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScoredNote:
    """A note and its relevance score for one question."""

    note: Note
    score: float


def _normalise(text: str) -> str:
    # "P&L" is a real note title in this vault; keep a plain "pl" token so it
    # competes with questions like "How did our P&L look?".
    if re.search(r"\bp\s*&\s*l\b", text, re.IGNORECASE):
        text += " pl"
    return text.replace("&", " and ").replace("+", " plus ")


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens, minus common filler words."""
    tokens: list[str] = []
    for match in TOKEN_RE.finditer(_normalise(text)):
        token = match.group(0).lower()
        if len(token) >= 2 and token not in STOPWORDS:
            tokens.append(token)
    return tokens


def _terms(text: str) -> set[str]:
    return set(tokenize(text))


def score_notes(
    notes: list[Note],
    question: str,
    history: list[str] | None = None,
    limit: int = 6,
) -> list[ScoredNote]:
    """Rank notes by keyword overlap.

    Title matches weigh the most, then titles/paths, groups, and body text.
    Previous user questions are included with a lower weight so follow-up
    questions such as "what about wholesale?" still find relevant notes.
    """
    current_terms = Counter(tokenize(question))
    history_terms: Counter[str] = Counter()
    for previous in (history or [])[-2:]:
        history_terms.update(tokenize(previous))

    if not current_terms and not history_terms:
        return []

    query_phrase = " ".join(dict.fromkeys(tokenize(question)))
    recency_wanted = bool(RECENCY_RE.search(question))
    now = datetime.now().astimezone()
    scored: list[ScoredNote] = []

    for note in notes:
        title_terms = _terms(f"{note.title} {note.label}")
        path_terms = _terms(note.path)
        group_terms = _terms(note.group)
        body_terms = _terms(note.text)
        title_lower = f"{note.title} {note.label}".lower()
        body_lower = note.text.lower()
        score = 0.0

        for term, count in current_terms.items():
            weight = 1.0 + min(count - 1, 3) * 0.2
            if term in title_terms:
                score += 6.0 * weight
            elif term in path_terms:
                score += 2.5 * weight
            if term in group_terms:
                score += 1.5 * weight
            if term in body_terms:
                score += 1.0 * weight

        for term, _count in history_terms.items():
            if term in current_terms:
                continue
            if term in title_terms:
                score += 1.5
            elif term in path_terms:
                score += 0.6
            if term in group_terms:
                score += 0.4
            if term in body_terms:
                score += 0.3

        if len(query_phrase) >= 4:
            if query_phrase in title_lower:
                score += 4.0
            if query_phrase in body_lower:
                score += 3.0

        # Fresh captures should win a tie when the question asks for the
        # latest material. Only apply the boost to notes that already match,
        # so "latest" alone cannot pull in unrelated notes.
        if score > 0 and recency_wanted and note.created is not None:
            created = note.created
            if created.tzinfo is None:
                created = created.astimezone()
            age_days = (now - created.astimezone()).total_seconds() / 86400
            if age_days < 0:
                age_days = 0.0
            if age_days <= 30:
                score += 2.5 * (1 - age_days / 30)

        if score > 0:
            scored.append(ScoredNote(note=note, score=round(score, 3)))

    scored.sort(key=lambda item: (-item.score, item.note.id))
    return scored[: max(1, limit)]
