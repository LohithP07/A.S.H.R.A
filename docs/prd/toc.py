"""Fill the contents page numbers and stamp PDF metadata + bookmarks.

Local-only utility for docs/prd. No network access.
"""
import io
import re
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).parent
HTML = ROOT / "ashra-prd.html"
PDF = ROOT / "ashra-prd.pdf"

SECTIONS = [
    ("sec1", "1. Executive summary", "Executive summary"),
    ("sec2", "2. Product definition", "Product definition"),
    ("sec3", "3. Scope and release boundaries", "Scope and release boundaries"),
    ("sec4", "4. User journeys", "User journeys"),
    ("sec5", "5. System architecture", "System architecture"),
    ("sec6", "6. Privacy and security model", "Privacy and security model"),
    ("sec7", "7. Connectors and channels", "Connectors and channels"),
    ("sec8", "8. Memory, life model, and Galaxy", "Memory, life model, and Galaxy"),
    ("sec9", "9. Tasks and automation", "Tasks and automation"),
    ("sec10", "10. Subagents and model lanes", "Subagents and model lanes"),
    ("sec11", "11. Interfaces and surfaces", "Interfaces and surfaces"),
    ("sec12", "12. Data model", "Data model"),
    ("sec13", "13. Functional requirements", "Functional requirements"),
    ("sec14", "14. Non-functional requirements", "Non-functional requirements"),
    ("sec15", "15. Roadmap and milestones", "Roadmap and milestones"),
    ("sec16", "16. Risks and mitigations", "Risks and mitigations"),
    ("sec17", "17. Acceptance criteria and metrics", "Acceptance criteria and metrics"),
    ("sec18", "18. Decisions log and open questions", "Decisions log and open questions"),
    ("appA", "A. Glossary", "Glossary"),
    ("appB", "B. Configuration reference", "Configuration reference"),
    ("appC", "C. Phase history", "Phase history"),
]


def page_map(reader):
    texts = [(p.extract_text() or "") for p in reader.pages]
    found = {}
    for key, _label, needle in SECTIONS:
        for idx in range(3, len(texts)):  # skip cover, doc control, contents
            if needle in texts[idx]:
                found[key] = idx + 1  # 1-based PDF page
                break
        else:
            raise SystemExit(f"Could not locate section {key} ({needle})")
    return found


def fill_contents(pages):
    html = io.open(HTML, encoding="utf-8").read()
    for key, _label, _needle in SECTIONS:
        pattern = re.compile(r'(data-pg="%s">)(\d+)(<)' % re.escape(key))
        if not pattern.search(html):
            raise SystemExit(f"Missing contents placeholder for {key}")
        html = pattern.sub(lambda m: m.group(1) + str(pages[key]) + m.group(3), html)
    io.open(HTML, "w", encoding="utf-8", newline="\n").write(html)


def stamp(pages):
    reader = PdfReader(str(PDF))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    writer.add_metadata({
        "/Title": "A.S.H.R.A - Product Requirements Document",
        "/Author": "Lohit",
        "/Subject": "Local-first personal assistant for Windows 11: scope, privacy model, connectors, tasks, subagents, and roadmap.",
        "/Keywords": "A.S.H.R.A, PRD, local-first, personal assistant, privacy, Telegram, Gmail, calendar, tasks, subagents",
        "/Creator": "A.S.H.R.A local print pipeline",
        "/Producer": "Chrome DevTools Protocol + pypdf",
    })

    writer.add_outline_item("Cover", 0)
    writer.add_outline_item("Document control", 1)
    writer.add_outline_item("Contents", 2)
    for key, label, _needle in SECTIONS:
        writer.add_outline_item(label, pages[key] - 1)

    tmp = PDF.with_suffix(".tmp.pdf")
    with open(tmp, "wb") as fh:
        writer.write(fh)
    tmp.replace(PDF)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    reader = PdfReader(str(PDF))
    pages = page_map(reader)
    for key, _label, _needle in SECTIONS:
        print(f"{key:6s} -> page {pages[key]}")
    if mode in ("all", "toc"):
        fill_contents(pages)
        print("contents updated")
    if mode in ("all", "meta"):
        stamp(pages)
        print("metadata and bookmarks written")
