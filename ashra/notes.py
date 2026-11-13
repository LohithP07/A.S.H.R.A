"""Note loading for retrieval.

build.py already owns markdown discovery and cleaning.  Reusing it here keeps
the graph and the chat brain looking at exactly the same notes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import build


@dataclass(frozen=True)
class Note:
    """A single note available to the brain."""

    id: int
    label: str
    title: str
    group: str
    path: str
    excerpt: str
    text: str
    words: int
    created: datetime | None = None
    modified: datetime | None = None
    source: str = ""


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def load_notes() -> list[Note]:
    """Load the current notes from the project's notes/ folder."""
    discovered = build.discover_notes()
    notes: list[Note] = []

    for item in discovered:
        try:
            mtime = (build.NOTES_DIR / item["path"]).stat().st_mtime
            modified = datetime.fromtimestamp(mtime).astimezone()
        except OSError:
            modified = None

        notes.append(
            Note(
                id=int(item["id"]),
                label=str(item["label"]),
                title=str(item["title"]),
                group=str(item["group"]),
                path=item["path"].as_posix(),
                excerpt=str(item["excerpt"]),
                text=build.clean_markdown(item["raw"]),
                words=int(item["words"]),
                created=_parse_timestamp(str(item.get("created", ""))),
                modified=modified,
                source=str(item.get("source", "")),
            )
        )

    return notes
