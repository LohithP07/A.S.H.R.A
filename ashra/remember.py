"""Capture and maintain spoken notes.

A capture is deterministic: it needs no model provider, so "remember that …"
still works when the brain is offline.  Notes are written under
notes/captures/, linked to their closest existing note with a wikilink, and the
galaxy is rebuilt with the same functions build.py uses for a full rebuild.

The same module owns the small set of safety operations: forgetting the last
capture, renaming it, and appending to it.  Destructive work is deliberately
limited to notes/captures/ so a command can never reach the rest of the vault.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import build

from .notes import Note, load_notes
from .retrieval import score_notes

CAPTURES_FOLDER = "captures"
MAX_CAPTURE_CHARS = 2000
MIN_CAPTURE_CHARS = 3
TITLE_WORDS = 8
TITLE_CHARS = 72

# Serialise vault writes: ThreadingHTTPServer can serve two at once, and a
# torn graph-data.js would be worse than a slightly slower save.
_WRITE_LOCK = threading.Lock()

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_RELATED_RE = re.compile(r"^Related:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_RELATED_LINE_RE = re.compile(r"^\s*Related\s*:", re.IGNORECASE)

# The viewer strips this before posting, but the endpoint is public to the
# local machine, so the same courtesy is repeated here.
_PREFIX_RE = re.compile(
    r"^(?:a\.?\s*s\.?\s*h\.?\s*r\.?\s*a[,\s]+)?(?:please\s+)?"
    r"(?:"
    r"remember(?:\s+that)?"
    r"|(?:make|write|take|save)\s+(?:a\s+)?note(?:\s+that)?"
    r"|note\s+that"
    r"|(?:save|jot\s+down|log)(?:\s+that)?"
    r")"
    r"[:\s]+",
    re.IGNORECASE,
)

# Words that make a truncated title read badly at the end.
_WEAK_TAIL = {
    "a", "an", "and", "as", "at", "before", "but", "by", "for", "from", "in",
    "is", "of", "on", "or", "our", "that", "the", "to", "was", "we", "with",
}


@dataclass(frozen=True)
class RememberResult:
    title: str
    path: str
    node: int | None
    related: dict | None
    graph: dict
    reply: str
    created: str = ""
    source: str = ""


def _clean(text: str) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    cleaned = _PREFIX_RE.sub("", cleaned, count=1).strip()
    return cleaned


def _derive_title(text: str) -> str:
    words = text.split()[:TITLE_WORDS]
    while len(words) > 3 and words[-1].strip(".,;:!?—-").lower() in _WEAK_TAIL:
        words.pop()

    title = " ".join(words).strip(" .,;:!?—-")
    if not title:
        title = "Untitled Capture"
    # Brackets, pipes, and hashes would break the [[wikilink]] we write.
    title = re.sub(r"[\[\]|#]+", " ", title)
    title = " ".join(title.split()) or "Untitled Capture"
    if text.isupper():
        title = title.lower()
    title = title[:1].upper() + title[1:]
    if len(title) > TITLE_CHARS:
        title = title[:TITLE_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return title


def _clean_title(text: str) -> str:
    """Normalise a requested rename without accepting link-breaking characters."""
    cleaned = " ".join(str(text or "").split()).strip(" .,;:!?—-")
    cleaned = re.sub(r"[\[\]|#]+", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) < 3:
        raise ValueError("Give the note a longer title.")
    if len(cleaned) > TITLE_CHARS:
        cleaned = cleaned[:TITLE_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cleaned[:1].upper() + cleaned[1:]


def _unique_title(title: str, taken: set[str]) -> str:
    candidate = title
    suffix = 2
    while build.normalize_key(candidate) in taken:
        candidate = f"{title} ({suffix})"
        suffix += 1
    return candidate


def _slugify(title: str) -> str:
    ascii_title = (
        unicodedata.normalize("NFKD", title)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = _SLUG_RE.sub("-", ascii_title.lower()).strip("-")
    return (slug[:60].rstrip("-") or "capture")


def _unique_slug(slug: str, folder: Path) -> str:
    candidate = slug
    suffix = 2
    while (folder / f"{candidate}.md").exists():
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def _append_related(raw: str, title: str) -> str:
    """Add [[title]] to the note's Related line, creating it when absent."""
    link = f"[[{title}]]"
    matches = list(_RELATED_RE.finditer(raw))

    if matches:
        last = matches[-1]
        existing = last.group(1).strip()
        if link.lower() in existing.lower():
            return raw
        if existing:
            line = f"Related: {existing.rstrip(' .,;')}, {link}"
        else:
            line = f"Related: {link}"
        return raw[: last.start()] + line + raw[last.end():]

    return raw.rstrip() + f"\n\nRelated: {link}\n"


def _remove_related_link(raw: str, title: str) -> str:
    """Remove a [[title]] link from Related lines, including its punctuation."""
    link_re = re.compile(
        r"\[\[\s*" + re.escape(title) + r"\s*(?:\|[^\]]*)?\]\]",
        re.IGNORECASE,
    )
    kept: list[str] = []
    changed = False

    for line in raw.splitlines():
        if not _RELATED_LINE_RE.match(line):
            kept.append(line)
            continue

        replaced = link_re.sub("", line)
        if replaced == line:
            kept.append(line)
            continue

        changed = True
        replaced = re.sub(r"\s*,\s*(?=,|$)", "", replaced)
        replaced = re.sub(r",\s*,+", ",", replaced)
        replaced = re.sub(r":[ \t]*,", ":", replaced)
        replaced = re.sub(r"[ \t]{2,}", " ", replaced).rstrip()
        replaced = re.sub(r"related\s*:\s*$", "", replaced, flags=re.IGNORECASE)
        if replaced.strip():
            kept.append(replaced)

    if not changed:
        return raw

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return f"{text}\n" if text else ""


def _replace_wikilink(raw: str, old_title: str, new_title: str) -> str:
    """Point every [[old title]] at the renamed note, keeping display labels."""
    pattern = re.compile(
        r"\[\[\s*" + re.escape(old_title) + r"\s*(?:\|([^\]]*))?\]\]",
        re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        label = (match.group(1) or "").strip()
        return f"[[{new_title}|{label}]]" if label else f"[[{new_title}]]"

    return pattern.sub(replacement, raw)


def _capture_body(
    title: str,
    text: str,
    related_title: str | None,
    created: datetime,
    source: str,
) -> str:
    lines = [
        "---",
        f"created: {created.isoformat(timespec='seconds')}",
        f"source: {source}",
        "---",
        "",
        f"# {title}",
        "",
        text,
        "",
    ]
    if related_title:
        lines.append(f"Related: [[{related_title}]]")
        lines.append("")
    return "\n".join(lines)


def _insert_body(raw: str, text: str) -> str:
    """Append a paragraph before the trailing Related line, if there is one."""
    lines = raw.rstrip().splitlines()
    index = len(lines)
    for position in range(len(lines) - 1, -1, -1):
        if _RELATED_LINE_RE.match(lines[position]):
            index = position
            break

    before = lines[:index]
    after = lines[index:]
    while before and not before[-1].strip():
        before.pop()
    while after and not after[0].strip():
        after.pop(0)

    body = before + ["", text, ""] + after
    return "\n".join(body).rstrip() + "\n"


def _capture_notes(notes: list[Note]) -> list[Note]:
    prefix = f"{CAPTURES_FOLDER}/"
    return [note for note in notes if note.path.startswith(prefix)]


def _find_capture(
    base_dir: Path,
    notes: list[Note],
    path: str | None,
    last: bool,
) -> Note:
    captures = _capture_notes(notes)
    if path:
        normalized = str(path).strip().replace("\\", "/").lstrip("/")
        if normalized.startswith("notes/"):
            normalized = normalized[len("notes/"):]
        if not normalized.startswith(f"{CAPTURES_FOLDER}/"):
            raise ValueError("I only work with captures, sir.")
        match = next((note for note in captures if note.path == normalized), None)
        if match is None:
            raise ValueError("That capture is not in the vault.")
        return match

    if not captures:
        raise ValueError("There is no capture to work with yet.")

    if not last:
        raise ValueError("Name a capture, or ask for the last one.")

    def stamp(note: Note) -> float:
        if note.created is not None:
            created = note.created
            if created.tzinfo is None:
                created = created.astimezone()
            return created.timestamp()
        file_path = base_dir / "notes" / note.path
        try:
            return file_path.stat().st_mtime
        except OSError:
            return 0.0

    return max(captures, key=stamp)


def _capture_path(base_dir: Path, path: str) -> Path:
    """Resolve a capture path and prove it stays inside notes/captures/."""
    notes_dir = (base_dir / "notes").resolve()
    captures_dir = (notes_dir / CAPTURES_FOLDER).resolve()
    target = (notes_dir / path).resolve()
    if captures_dir not in target.parents or not target.is_file():
        raise ValueError("That capture is not in the vault.")
    return target


def _rebuild(rel_path: str | None = None) -> tuple[dict, dict | None]:
    rebuilt = build.discover_notes()
    links = build.build_graph(rebuilt)
    build.write_output(rebuilt, links)
    graph = build.graph_payload(rebuilt, links)

    if rel_path is None:
        return graph, None

    node = next(
        (item for item in graph["nodes"] if item["path"] == rel_path),
        None,
    )
    if node is None:  # Should be impossible after a successful rebuild.
        raise RuntimeError("The vault changed but the note could not be found.")
    return graph, node


def remember(base_dir: Path, text: str, source: str = "typed") -> RememberResult:
    """Write one capture, rebuild the graph, and describe where it landed."""
    cleaned = _clean(text)
    if len(cleaned) < MIN_CAPTURE_CHARS:
        raise ValueError("Tell me something worth remembering first.")
    if len(cleaned) > MAX_CAPTURE_CHARS:
        raise ValueError(
            f"That note is too long. Keep captures under {MAX_CAPTURE_CHARS:,} characters."
        )

    origin = "voice" if str(source or "").lower() == "voice" else "typed"
    created = datetime.now().astimezone().replace(microsecond=0)
    captures = base_dir / "notes" / CAPTURES_FOLDER

    # Everything from note scan to graph write happens under one lock, so two
    # simultaneous captures cannot choose the same title or append in a race.
    with _WRITE_LOCK:
        notes = load_notes()
        taken = set()
        for note in notes:
            taken.add(build.normalize_key(note.title))
            taken.add(build.normalize_key(note.label))

        title = _unique_title(_derive_title(cleaned), taken)
        matches = score_notes(notes, cleaned, limit=1)
        related = matches[0].note if matches else None
        related_title = related.title if related else None

        captures.mkdir(parents=True, exist_ok=True)
        slug = _unique_slug(_slugify(title), captures)
        path = captures / f"{slug}.md"
        path.write_text(
            _capture_body(title, cleaned, related_title, created, origin),
            encoding="utf-8",
        )

        if related is not None:
            related_path = base_dir / "notes" / related.path
            original = related_path.read_text(encoding="utf-8", errors="replace")
            updated = _append_related(original, title)
            if updated != original:
                related_path.write_text(updated, encoding="utf-8")

        rel_path = f"{CAPTURES_FOLDER}/{slug}.md"
        graph, node = _rebuild(rel_path)

    related_payload = None
    if related is not None:
        related_node = next(
            (item for item in graph["nodes"] if item["path"] == related.path),
            None,
        )
        if related_node is not None:
            related_payload = {
                "id": related_node["id"],
                "label": related_node["label"],
                "title": related_node["title"],
                "path": related_node["path"],
            }

    if related_payload:
        reply = (
            f'Noted, sir. "{title}" is in the vault, filed beside '
            f'{related_payload["label"]}.'
        )
    else:
        reply = f'Noted, sir. "{title}" is in the vault.'

    return RememberResult(
        title=title,
        path=rel_path,
        node=int(node["id"]),
        related=related_payload,
        graph=graph,
        reply=reply,
        created=created.isoformat(),
        source=origin,
    )


def forget(
    base_dir: Path,
    path: str | None = None,
    last: bool = False,
) -> RememberResult:
    """Delete one capture and remove the wikilink it left behind."""
    with _WRITE_LOCK:
        notes = load_notes()
        target = _find_capture(base_dir, notes, path, last)
        target_path = _capture_path(base_dir, target.path)
        title = target.title
        deleted_path = target.path

        target_path.unlink()

        # A capture is linked from its related note; leaving [[Title]] behind
        # would create a dangling reference on the next build.
        for note in notes:
            if note.path == deleted_path:
                continue
            other = base_dir / "notes" / note.path
            if not other.is_file():
                continue
            original = other.read_text(encoding="utf-8", errors="replace")
            updated = _remove_related_link(original, title)
            if updated != original:
                other.write_text(updated, encoding="utf-8")

        graph, _ = _rebuild()

    return RememberResult(
        title=title,
        path=deleted_path,
        node=None,
        related=None,
        graph=graph,
        reply=f'Deleted, sir. "{title}" is out of the vault.',
    )


def rename(
    base_dir: Path,
    title: str,
    path: str | None = None,
    last: bool = False,
) -> RememberResult:
    """Rename a capture's title, file, and every wikilink that points at it."""
    new_title = _clean_title(title)

    with _WRITE_LOCK:
        notes = load_notes()
        target = _find_capture(base_dir, notes, path, last)
        old_title = target.title
        old_path = _capture_path(base_dir, target.path)

        taken = {
            build.normalize_key(note.title)
            for note in notes
            if note.path != target.path
        }
        taken.update(
            build.normalize_key(note.label)
            for note in notes
            if note.path != target.path
        )
        new_title = _unique_title(new_title, taken)

        raw = old_path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*#\s+", raw, re.MULTILINE):
            raw = re.sub(
                r"^\s*#\s+.*$",
                f"# {new_title}",
                raw,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            raw = f"# {new_title}\n\n{raw.lstrip()}"

        capture_dir = old_path.parent
        new_slug = _unique_slug(_slugify(new_title), capture_dir)
        new_path = capture_dir / f"{new_slug}.md"
        new_path.write_text(raw, encoding="utf-8")
        if new_path != old_path:
            old_path.unlink()

        # Update the explicit links other notes hold to the old title.
        for note in notes:
            if note.path == target.path:
                continue
            other = base_dir / "notes" / note.path
            if not other.is_file():
                continue
            original = other.read_text(encoding="utf-8", errors="replace")
            updated = _replace_wikilink(original, old_title, new_title)
            if updated != original:
                other.write_text(updated, encoding="utf-8")

        rel_path = f"{CAPTURES_FOLDER}/{new_slug}.md"
        graph, node = _rebuild(rel_path)

    return RememberResult(
        title=new_title,
        path=rel_path,
        node=int(node["id"]),
        related=None,
        graph=graph,
        reply=f'Renamed, sir. "{old_title}" is now "{new_title}".',
        created=target.created.isoformat() if target.created else "",
        source=target.source,
    )


def append(
    base_dir: Path,
    text: str,
    path: str | None = None,
    last: bool = False,
) -> RememberResult:
    """Add a paragraph to a capture without changing its title."""
    cleaned = " ".join(str(text or "").split()).strip()
    if len(cleaned) < MIN_CAPTURE_CHARS:
        raise ValueError("Tell me what to add first.")
    if len(cleaned) > MAX_CAPTURE_CHARS:
        raise ValueError(
            f"That addition is too long. Keep it under {MAX_CAPTURE_CHARS:,} characters."
        )

    with _WRITE_LOCK:
        notes = load_notes()
        target = _find_capture(base_dir, notes, path, last)
        target_path = _capture_path(base_dir, target.path)
        raw = target_path.read_text(encoding="utf-8", errors="replace")
        target_path.write_text(_insert_body(raw, cleaned), encoding="utf-8")

        graph, node = _rebuild(target.path)

    return RememberResult(
        title=target.title,
        path=target.path,
        node=int(node["id"]),
        related=None,
        graph=graph,
        reply=f'Added to "{target.title}", sir.',
        created=target.created.isoformat() if target.created else "",
        source=target.source,
    )
