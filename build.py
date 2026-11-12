#!/usr/bin/env python3
"""
build.py - turn a folder of markdown notes into a knowledge-graph data file.

Scans every .md file under ./notes, then writes ./viewer/graph-data.js:

    const GRAPH = { nodes: [...], links: [...] };

Every node gets:
    id      - numeric index equal to its position in the nodes array
    label   - a readable label derived from the filename
    group   - the note's folder (top-level folder name, prettified)
    excerpt - roughly 700 characters of plain-text note content
    path    - path relative to the notes folder (handy extra metadata)
    title   - the note's first H1 heading, if it has one
    words   - approximate word count

Links are created when one note mentions another note's title, or when a note
contains a [[wikilink]] that resolves to another note.  Duplicate pairs are
collapsed, keeping the stronger reason ("wikilink" > "mention").

Standard library only.  Run:  python build.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
NOTES_DIR = BASE_DIR / "notes"
VIEWER_DIR = BASE_DIR / "viewer"
OUTPUT_FILE = VIEWER_DIR / "graph-data.js"

EXCERPT_LENGTH = 700

# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL)
FRONTMATTER_FIELDS_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
HTML_RE = re.compile(r"<[^>]+>")
TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|-]{3,}\|?\s*$", re.MULTILINE)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
CHECKBOX_RE = re.compile(r"\[[ xX]\]\s*")
EMPHASIS_RE = re.compile(r"[*_~`]+")
ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|])")
SPACE_RE = re.compile(r"\s+")

SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "of",
    "on", "or", "the", "to", "vs", "with",
}
SPECIAL_WORDS = {
    "pl": "P&L",
    "faq": "FAQ",
    "seo": "SEO",
    "pos": "POS",
    "diy": "DIY",
    "hr": "HR",
}


def title_case(text: str) -> str:
    """Human-friendly title casing for a filename stem or folder name."""
    words = re.split(r"[-_\s]+", text.strip())
    out: list[str] = []
    for index, word in enumerate(words):
        if not word:
            continue
        lower = word.lower()
        if lower in SPECIAL_WORDS:
            out.append(SPECIAL_WORDS[lower])
        elif word.isupper() and len(word) > 1:
            out.append(word)
        elif index > 0 and lower in SMALL_WORDS:
            out.append(lower)
        else:
            out.append(lower[:1].upper() + lower[1:])
    return " ".join(out)


def strip_frontmatter(text: str) -> str:
    return FRONTMATTER_RE.sub("", text, count=1)


def parse_frontmatter(text: str) -> dict[str, str]:
    """Read the simple `key: value` frontmatter used by captures.

    Deliberately not a full YAML parser: captures only need flat scalar fields
    such as `created` and `source`, and staying dependency-free keeps build.py
    runnable with a plain Python install.
    """
    match = FRONTMATTER_FIELDS_RE.match(text)
    if not match:
        return {}

    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if not key:
            continue
        fields[key] = value.strip().strip('"').strip("'")
    return fields


def first_h1(text: str) -> str | None:
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", line)
        if match:
            return match.group(1).strip()
    return None


def strip_first_h1(text: str) -> str:
    """Remove the leading H1 so panel excerpts don't repeat the node label."""
    return re.sub(r"^\s*#\s+.*?(?:\n|$)", "", text, count=1, flags=re.MULTILINE)


def normalize_key(value: str) -> str:
    """
    Normalize a wikilink target or title for comparison:
    lower-case, no punctuation, single spaces, last path component only.
    """
    value = value.strip().split("|")[0].strip()
    value = value.split("#")[0].strip()
    value = value.replace("\\", "/").rstrip("/")
    if value.lower().endswith(".md"):
        value = value[:-3]
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return SPACE_RE.sub(" ", value).strip().lower()


def clean_markdown(text: str) -> str:
    """Turn a markdown note into readable, compact plain text."""
    text = strip_frontmatter(text)
    text = FENCE_RE.sub(" ", text)
    text = INLINE_CODE_RE.sub(r"\1", text)
    text = IMAGE_RE.sub(r"\1", text)
    text = MD_LINK_RE.sub(r"\1", text)
    text = WIKILINK_RE.sub(lambda m: m.group(1).split("|")[0], text)
    text = HTML_RE.sub(" ", text)

    # Drop table separator rows, then flatten the remaining table pipes.
    text = TABLE_RULE_RE.sub(" ", text)
    text = text.replace("|", " ")

    text = HEADING_RE.sub("", text)
    text = BLOCKQUOTE_RE.sub("", text)
    text = LIST_RE.sub("", text)
    text = CHECKBOX_RE.sub("", text)
    text = EMPHASIS_RE.sub("", text)
    text = ESCAPE_RE.sub(r"\1", text)
    text = SPACE_RE.sub(" ", text)
    return text.strip()


def excerpt_from(raw: str) -> str:
    """Around 700 characters of prose, cut at a word boundary."""
    body = strip_first_h1(strip_frontmatter(raw))
    clean = clean_markdown(body)
    if len(clean) <= EXCERPT_LENGTH:
        return clean
    cut = clean[: EXCERPT_LENGTH - 1]
    # Prefer a sentence boundary when one is close to the limit.
    for mark in (". ", "! ", "? "):
        pos = cut.rfind(mark)
        if pos > EXCERPT_LENGTH * 0.94:
            cut = cut[: pos + 1]
            break
    else:
        pos = cut.rfind(" ")
        if pos > EXCERPT_LENGTH * 0.55:
            cut = cut[:pos]
    return cut.rstrip(" ,;:-") + "…"


def mention_regex(alias: str) -> re.Pattern[str]:
    """Match a title alias, tolerating spaces, hyphens, or underscores."""
    parts = [re.escape(part) for part in re.split(r"[\s_-]+", alias) if part]
    core = r"[\s_-]+".join(parts)
    return re.compile(rf"(?<![\w]){core}(?![\w])", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Scan notes
# ---------------------------------------------------------------------------

def discover_notes() -> list[dict]:
    if not NOTES_DIR.is_dir():
        raise FileNotFoundError(f"Notes folder not found: {NOTES_DIR}")

    paths = sorted(
        (p for p in NOTES_DIR.rglob("*") if p.is_file() and p.suffix.lower() == ".md"),
        key=lambda p: p.relative_to(NOTES_DIR).as_posix().lower(),
    )
    notes: list[dict] = []

    for path in paths:
        raw = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(NOTES_DIR)
        stem = path.stem
        label = title_case(stem)
        h1 = first_h1(strip_frontmatter(raw))
        title = h1 or label
        meta = parse_frontmatter(raw)

        # Folder becomes the group. Root-level notes go to "General".
        folder = rel.parent.as_posix()
        group = "General" if folder == "." else " / ".join(
            title_case(part) for part in rel.parent.parts
        )

        aliases = {label, title, stem.replace("-", " ").replace("_", " "), stem}
        # The H1 may carry a subtitle after a dash/colon; keep the short half too.
        for separator in (" - ", " — ", ": "):
            if separator in title:
                left, right = title.split(separator, 1)
                if 3 <= len(left) <= 70:
                    aliases.add(left.strip())
                if 3 <= len(right) <= 70:
                    aliases.add(right.strip())
        aliases = {a.strip() for a in aliases if a.strip() and len(a.strip()) >= 3}

        notes.append(
            {
                "id": len(notes),  # numeric id == position in nodes array
                "path": rel,
                "label": label,
                "group": group,
                "title": title,
                "raw": strip_frontmatter(raw),
                "excerpt": excerpt_from(raw),
                "words": len(clean_markdown(raw).split()),
                "aliases": aliases,
                "created": meta.get("created", ""),
                "source": meta.get("source", ""),
            }
        )

    return notes


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

LINK_RANK = {"mention": 1, "wikilink": 2}
LINK_LABEL = {"mention": "mentions title", "wikilink": "wikilink"}


def build_graph(notes: list[dict]) -> list[dict]:
    # Map every alias to a note id. First note wins on the rare collision.
    alias_map: dict[str, int] = {}
    for note in notes:
        for alias in sorted(note["aliases"], key=len, reverse=True):
            key = normalize_key(alias)
            if key and key not in alias_map:
                alias_map[key] = note["id"]

    pair_kind: dict[tuple[int, int], str] = {}

    def add_link(a: int, b: int, kind: str) -> None:
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        if LINK_RANK[kind] > LINK_RANK.get(pair_kind.get(key, ""), 0):
            pair_kind[key] = kind

    # 1) Plain-title mentions, and direct wikilinks.
    compiled: list[tuple[int, list[re.Pattern[str]]]] = []
    for note in notes:
        patterns = [mention_regex(alias) for alias in note["aliases"]]
        compiled.append((note["id"], patterns))

    for note in notes:
        body = note["raw"]

        for other_id, patterns in compiled:
            if other_id == note["id"]:
                continue
            if any(pattern.search(body) for pattern in patterns):
                add_link(note["id"], other_id, "mention")

        for match in WIKILINK_RE.finditer(body):
            target = match.group(1).split("|")[0].strip()
            key = normalize_key(target)
            if not key:
                continue
            target_id = alias_map.get(key)
            if target_id is not None and target_id != note["id"]:
                add_link(note["id"], target_id, "wikilink")

    return [
        {"source": a, "target": b, "kind": LINK_LABEL[kind]}
        for (a, b), kind in sorted(pair_kind.items())
    ]


# ---------------------------------------------------------------------------
# Write output
# ---------------------------------------------------------------------------

def graph_payload(notes: list[dict], links: list[dict]) -> dict:
    """The exact JSON-ready graph shared by the builder and /remember."""
    nodes = [
        {
            "id": note["id"],
            "label": note["label"],
            "group": note["group"],
            "excerpt": note["excerpt"],
            "path": note["path"].as_posix(),
            "title": note["title"],
            "words": note["words"],
            "created": note.get("created", ""),
            "source": note.get("source", ""),
        }
        for note in notes
    ]
    return {"nodes": nodes, "links": links}


def write_output(notes: list[dict], links: list[dict]) -> None:
    VIEWER_DIR.mkdir(parents=True, exist_ok=True)

    graph = graph_payload(notes, links)
    nodes = graph["nodes"]
    payload = json.dumps(graph, ensure_ascii=False, indent=2)

    header = (
        "/* Generated by build.py - do not edit by hand.\n"
        f" * {len(nodes)} notes, {len(links)} links.\n"
        " * Every node id is its index in the nodes array. */\n"
    )
    OUTPUT_FILE.write_text(
        header + "const GRAPH = " + payload + ";\n\nwindow.GRAPH = GRAPH;\n",
        encoding="utf-8",
    )


def main() -> int:
    try:
        notes = discover_notes()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not notes:
        print(f"error: no .md files found under {NOTES_DIR}", file=sys.stderr)
        return 1

    links = build_graph(notes)
    write_output(notes, links)

    groups = sorted({note["group"] for note in notes})
    average = sum(len(note["excerpt"]) for note in notes) / len(notes)
    print(f"Scanned {len(notes)} notes in {len(groups)} groups: {', '.join(groups)}")
    print(f"Created {len(links)} unique links")
    print(f"Average excerpt length: {average:.0f} characters")
    print(f"Wrote {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
