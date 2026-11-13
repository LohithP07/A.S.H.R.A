"""The single privacy vocabulary for A.S.H.R.A.

Sensitivity is not a free-text field.  The PRD defines five classes and one
rule: classification is assigned at ingestion, inherited by derived records,
and never downgraded automatically.  Keeping the vocabulary in one module means
a connector, a memory, and the future model router cannot disagree about what
"private" means.

    public     web pages, documentation, general knowledge
    internal   personal notes, project plans, non-sensitive research
    private    email bodies, calendar detail, contacts, drafts, tasks
    sensitive  attachments, identity documents, private conversations
    secret     financial records, health information, passwords, tokens

A mixed request takes the highest class present.  When classification is
uncertain, choose the more private class and say so.
"""

from __future__ import annotations

import re

CLASSES = ("public", "internal", "private", "sensitive", "secret")

# Classes that must not leave the machine.
LOCAL_ONLY = ("private", "sensitive", "secret")

# Classes no model may read.
NO_MODEL = ("secret",)

_RANK = {name: index for index, name in enumerate(CLASSES)}

# Older records used a three-value vocabulary.  Map them forward, never back.
_LEGACY = {"normal": "internal"}
DEFAULT = "internal"

# A memory must never become a credential store.  These patterns catch the
# obvious shapes; they are also the first pass of the secret classifier.
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
    r"social\s+security|medical|diagnosis|prescription|salary|bank\s+account|"
    r"iban|sort\s+code|insurance\s+policy)\b",
    re.IGNORECASE,
)

# Contact detail is private even when the prose around it is ordinary.
_PRIVATE_RE = re.compile(
    r"(?:\b[\w.+-]+@[\w-]+\.[\w.-]+\b"
    r"|\b(?:\+\d{1,3}[\s-]?)?(?:\(?\d{3,5}\)?[\s-]?)\d{3}[\s-]?\d{3,4}\b)",
)


def parse(value: str | None, default: str = DEFAULT) -> str:
    """Return a valid class name.

    Unknown values raise ``ValueError`` rather than silently falling back, so a
    typo in configuration cannot quietly widen a privacy boundary.
    """
    raw = str(value if value is not None else default).strip().lower()
    raw = _LEGACY.get(raw, raw)
    if raw not in _RANK:
        allowed = ", ".join(CLASSES)
        raise ValueError(f"Unknown sensitivity '{value}'. Use one of: {allowed}.")
    return raw


def rank(value: str) -> int:
    return _RANK[parse(value)]


def highest(*values: str | None) -> str:
    """Return the most private class among the inputs."""
    present = [parse(value) for value in values if value is not None]
    if not present:
        return DEFAULT
    return max(present, key=lambda name: _RANK[name])


def is_local_only(value: str | None) -> bool:
    return parse(value) in LOCAL_ONLY


def is_elevated(value: str | None) -> bool:
    """True for anything above ``internal``; used by health signals."""
    return parse(value) in LOCAL_ONLY


def looks_secret(text: str) -> bool:
    """True when the text contains a credential-shaped string."""
    return bool(_SECRET_RE.search(str(text or "")))


def classify(text: str, *, default: str = DEFAULT) -> str:
    """Infer a class from content.

    Deliberately conservative: credentials are secret, money and health are
    sensitive, contact detail is private, everything else is internal.  The
    caller may pass a narrower default; it is never widened here.
    """
    body = str(text or "")
    if looks_secret(body):
        return "secret"
    if _SENSITIVE_RE.search(body):
        return "sensitive"
    if _PRIVATE_RE.search(body):
        return "private"
    return parse(default)
