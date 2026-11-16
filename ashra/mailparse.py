"""Deterministic email parsing, threading, classification, and ask extraction.

PRD section 7.2 splits the pipeline into rules first and the local model only
where language understanding is genuinely required:

    parse -> clean -> classify (rules) -> propose -> (local model, ambiguous only)

Everything in this module is pure: bytes in, records out, no network, no
database, no model.  That keeps the expensive and risky parts (IMAP, Ollama)
replaceable and makes the corpus tests cheap.

Two constraints are architectural, not stylistic:

* attachments are reduced to metadata at parse time (FR-E9) - their bytes are
  never retained, quoted, or sent anywhere;
* quoted history and signatures are removed before anything else sees the body,
  because a reply chain otherwise leaks other people's words into a summary.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

DEFAULT_BODY_CHARS = 20_000
MAX_ASKS = 3

# Labels use the PRD's own vocabulary (FR-E4).
REPLY_NEEDED = "reply_needed"
WAITING = "waiting"
INFORMATIONAL = "informational"
IGNORE = "ignore"
LABELS = (REPLY_NEEDED, WAITING, INFORMATIONAL, IGNORE)

_NO_REPLY_RE = re.compile(
    r"^(?:no-?reply|do-?not-?reply|notifications?|mailer-daemon|postmaster|"
    r"billing|receipts?|orders?|support-noreply|newsletter)",
    re.IGNORECASE,
)
_RECEIPT_SUBJECT_RE = re.compile(
    r"\b(?:receipt|invoice|order (?:confirmation|shipped|delivered)|"
    r"your (?:order|statement|subscription)|payment (?:received|confirmed)|"
    r"verification code|one[- ]time (?:code|password)|password reset)\b",
    re.IGNORECASE,
)
_NEWSLETTER_SUBJECT_RE = re.compile(
    r"\b(?:newsletter|digest|weekly roundup|this week in|issue #?\d+)\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"\?\s*$", re.MULTILINE)
_ASK_PATTERNS = (
    re.compile(
        r"\b(?:could|can|would|will) you\b[^.?!\n]{3,200}[.?!]",
        re.IGNORECASE,
    ),
    re.compile(r"\bplease\b[^.?!\n]{3,200}[.?!]", re.IGNORECASE),
    re.compile(
        r"\b(?:i|we) need (?:you to|this|that)\b[^.?!\n]{3,200}[.?!]",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:send|share|forward|review|sign|confirm|approve|book|schedule)\b"
        r"[^.?!\n]{3,160}[.?!]",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:are|is) (?:you|it|this)\b[^.?!\n]{3,160}\?", re.IGNORECASE),
    re.compile(r"\blet me know\b[^.?!\n]{0,160}[.?!]", re.IGNORECASE),
)
_QUOTE_HEADS = (
    re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^_{5,}$"),
    re.compile(r"^on .{5,120}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^from:\s.+$", re.IGNORECASE),
    re.compile(r"^sent from my \w+", re.IGNORECASE),
)
_WEEKDAYS = {
    name: index
    for index, name in enumerate(
        (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        )
    )
}


@dataclass(frozen=True)
class AttachmentMeta:
    """Metadata only.  Attachment bytes are never stored (FR-E9)."""

    filename: str
    content_type: str
    size: int

    def as_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
        }


@dataclass(frozen=True)
class ParsedMessage:
    """One message, cleaned and ready to classify."""

    message_id: str = ""
    subject: str = ""
    from_addr: str = ""
    from_name: str = ""
    to_addrs: tuple[str, ...] = ()
    cc_addrs: tuple[str, ...] = ()
    occurred_at: float | None = None
    references: tuple[str, ...] = ()
    in_reply_to: str = ""
    thread_id: str = ""
    body: str = ""
    attachments: tuple[AttachmentMeta, ...] = ()
    automated: bool = False
    list_id: str = ""
    unsubscribe: bool = False

    @property
    def body_chars(self) -> int:
        return len(self.body)

    @property
    def sender(self) -> str:
        return self.from_name or self.from_addr

    def as_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "subject": self.subject,
            "from_addr": self.from_addr,
            "from_name": self.from_name,
            "to_addrs": list(self.to_addrs),
            "cc_addrs": list(self.cc_addrs),
            "occurred_at": self.occurred_at,
            "references": list(self.references),
            "in_reply_to": self.in_reply_to,
            "thread_id": self.thread_id,
            "body": self.body,
            "attachments": [item.as_dict() for item in self.attachments],
            "automated": self.automated,
            "list_id": self.list_id,
            "unsubscribe": self.unsubscribe,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ParsedMessage":
        attachments = tuple(
            AttachmentMeta(
                filename=str(item.get("filename", "")),
                content_type=str(item.get("content_type", "")),
                size=int(item.get("size", 0) or 0),
            )
            for item in (data.get("attachments") or [])
            if isinstance(item, dict)
        )
        occurred = data.get("occurred_at")
        return cls(
            message_id=str(data.get("message_id", "")),
            subject=str(data.get("subject", "")),
            from_addr=str(data.get("from_addr", "")),
            from_name=str(data.get("from_name", "")),
            to_addrs=tuple(str(item) for item in (data.get("to_addrs") or [])),
            cc_addrs=tuple(str(item) for item in (data.get("cc_addrs") or [])),
            occurred_at=float(occurred) if occurred else None,
            references=tuple(str(item) for item in (data.get("references") or [])),
            in_reply_to=str(data.get("in_reply_to", "")),
            thread_id=str(data.get("thread_id", "")),
            body=str(data.get("body", "")),
            attachments=attachments,
            automated=bool(data.get("automated")),
            list_id=str(data.get("list_id", "")),
            unsubscribe=bool(data.get("unsubscribe")),
        )


@dataclass(frozen=True)
class Ask:
    """One extracted request and, when unambiguous, its due hint."""

    text: str
    due_at: float | None = None
    due_hint: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"text": self.text, "due_at": self.due_at, "due_hint": self.due_hint}


@dataclass(frozen=True)
class Classification:
    label: str
    confidence: float
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "confidence": round(float(self.confidence), 2),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# cleaning
# ---------------------------------------------------------------------------
def _decode(payload: bytes, charset: str | None) -> str:
    for candidate in (charset, "utf-8", "cp1252"):
        if not candidate:
            continue
        try:
            return payload.decode(candidate, errors="replace")
        except LookupError:
            continue
    return payload.decode("utf-8", errors="replace")


def html_to_text(markup: str) -> str:
    """Reduce HTML to readable text without a dependency."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]{2,}", " ", text)


def strip_quoted(text: str) -> str:
    """Cut a reply chain at the first recognisable quote header."""
    kept: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if any(pattern.match(stripped) for pattern in _QUOTE_HEADS):
            break
        kept.append(line)
    return "\n".join(kept)


def strip_signature(text: str) -> str:
    """Drop a conventional ``-- `` signature block."""
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "--":
            return "\n".join(lines[:index])
    return str(text or "")


def clean_body(text: str, limit: int = DEFAULT_BODY_CHARS) -> str:
    body = strip_signature(strip_quoted(text))
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[: max(0, int(limit))]


def normalise_subject(value: str) -> str:
    subject = str(value or "").strip()
    while True:
        stripped = re.sub(
            r"^\s*(?:re|fw|fwd|aw|sv)\s*(?:\[\d+\])?\s*:\s*", "", subject, flags=re.I
        )
        # A leading list tag ("[list] Pricing") is not part of the subject: it
        # is a mailing-list marker and would defeat thread grouping.
        stripped = re.sub(r"^\s*\[[^\]\n]{1,40}\]\s*", "", stripped)
        if stripped == subject:
            break
        subject = stripped
    return re.sub(r"\s+", " ", subject).strip()


def message_key(value: str) -> str:
    return str(value or "").strip().strip("<>").strip()


def thread_key(message_id: str, references: tuple[str, ...], in_reply_to: str) -> str:
    """Thread identity from References, then In-Reply-To, then the message id."""
    for candidate in (*references, in_reply_to, message_id):
        key = message_key(candidate)
        if key:
            return key
    return ""


def body_hash(body: str) -> str:
    return hashlib.sha256(str(body or "").encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def _addresses(message: Message, header: str) -> tuple[str, ...]:
    raw = message.get_all(header) or []
    values = getaddresses([str(item) for item in raw])
    return tuple(
        f"{name.strip()} <{addr.strip()}>" if name.strip() else addr.strip()
        for name, addr in values
        if addr.strip()
    )


def _first_address(message: Message, header: str) -> tuple[str, str]:
    raw = message.get_all(header) or []
    values = getaddresses([str(item) for item in raw])
    for name, addr in values:
        if addr.strip():
            return name.strip(), addr.strip()
    return "", ""


def _timestamp(message: Message) -> float | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _bodies(message: Message) -> tuple[str, list[AttachmentMeta]]:
    plain: list[str] = []
    markup: list[str] = []
    attachments: list[AttachmentMeta] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        content_type = str(part.get_content_type() or "application/octet-stream")
        if filename or "attachment" in disposition:
            size = 0
            try:
                payload = part.get_payload(decode=True)
                size = len(payload or b"")
            except (TypeError, ValueError, base64.binascii.Error):
                size = 0
            attachments.append(
                AttachmentMeta(
                    filename=str(filename or "attachment"),
                    content_type=content_type,
                    size=size,
                )
            )
            continue
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True)
        except (TypeError, ValueError, base64.binascii.Error):
            payload = None
        if payload is None:
            text = str(part.get_payload() or "")
        else:
            text = _decode(payload, part.get_content_charset())
        if content_type == "text/plain":
            plain.append(text)
        else:
            markup.append(text)
    if plain:
        return "\n".join(plain), attachments
    if markup:
        return "\n".join(html_to_text(item) for item in markup), attachments
    return "", attachments


def parse_message(raw: bytes, *, limit: int = DEFAULT_BODY_CHARS) -> ParsedMessage:
    """Parse one RFC 822 message into a cleaned :class:`ParsedMessage`."""
    message = BytesParser(policy=policy.default).parsebytes(bytes(raw or b""))
    name, address = _first_address(message, "From")
    body, attachments = _bodies(message)
    message_id = message_key(str(message.get("Message-ID") or ""))
    references = tuple(
        message_key(item) for item in re.findall(r"<[^>]+>", str(message.get("References") or ""))
    )
    in_reply_to = message_key(str(message.get("In-Reply-To") or ""))
    precedence = str(message.get("Precedence") or "").lower()
    auto_submitted = str(message.get("Auto-Submitted") or "").lower()
    list_id = str(message.get("List-Id") or "").strip()
    automated = bool(
        list_id
        or precedence in {"bulk", "junk", "list"}
        or (auto_submitted and auto_submitted != "no")
        or re.match(r"^(?:no-?reply|do-?not-?reply|mailer-daemon|postmaster)", address or "", re.I)
        is not None
    )
    cleaned = clean_body(body, limit=limit)
    unsubscribe = bool(
        message.get("List-Unsubscribe")
        or re.search(r"\bunsubscribe\b", cleaned, re.IGNORECASE)
    )
    return ParsedMessage(
        message_id=message_id,
        subject=normalise_subject(str(message.get("Subject") or "")),
        from_addr=address,
        from_name=name,
        to_addrs=_addresses(message, "To"),
        cc_addrs=_addresses(message, "Cc"),
        occurred_at=_timestamp(message),
        references=references,
        in_reply_to=in_reply_to,
        thread_id=thread_key(message_id, references, in_reply_to),
        body=cleaned,
        attachments=tuple(attachments),
        automated=automated,
        list_id=list_id,
        unsubscribe=unsubscribe,
    )


# ---------------------------------------------------------------------------
# classification (rules first; the model only sees the ambiguous remainder)
# ---------------------------------------------------------------------------
def classify(parsed: ParsedMessage) -> Classification:
    """Classify one message using deterministic rules only (PRD 7.2)."""
    if parsed.automated:
        return Classification(IGNORE, 0.95, "automated headers")
    if _NO_REPLY_RE.match(parsed.from_addr or "") and not parsed.attachments:
        return Classification(IGNORE, 0.9, "no-reply sender")
    if _RECEIPT_SUBJECT_RE.search(parsed.subject):
        return Classification(IGNORE, 0.85, "receipt or notification subject")
    if _NEWSLETTER_SUBJECT_RE.search(parsed.subject):
        return Classification(IGNORE, 0.8, "newsletter subject")
    if parsed.unsubscribe and len(parsed.body) < 1200:
        return Classification(IGNORE, 0.7, "bulk mail with unsubscribe link")
    asks = extract_asks(parsed.body)
    if asks:
        return Classification(REPLY_NEEDED, 0.75, f"{len(asks)} request(s) found")
    if _QUESTION_RE.search(parsed.body):
        return Classification(REPLY_NEEDED, 0.6, "direct question")
    if "?" in parsed.subject:
        return Classification(REPLY_NEEDED, 0.55, "question in subject")
    return Classification(INFORMATIONAL, 0.5, "no request detected")


def extract_asks(text: str) -> tuple[Ask, ...]:
    """Return short, imperative requests found in the body.

    Conservative by design: only sentence-shaped requests qualify, because a
    false task is worse than a missed one (the owner still has the thread).
    """
    body = str(text or "")
    if not body.strip():
        return ()
    found: list[Ask] = []
    seen: set[str] = set()
    for pattern in _ASK_PATTERNS:
        for match in pattern.finditer(body):
            sentence = re.sub(r"\s+", " ", match.group(0)).strip()
            sentence = sentence.strip("\"' ")
            if len(sentence) < 12 or len(sentence) > 200:
                continue
            normalised = sentence.lower().rstrip(".?! ")
            if len(normalised) < 12:
                continue
            # Overlapping patterns find the same request ("could you send X" and
            # "send X"); keep the longer, more informative phrasing only.
            if any(
                normalised == other or normalised in other or other in normalised
                for other in seen
            ):
                continue
            seen.add(normalised)
            due_at, hint = parse_due(sentence)
            found.append(Ask(text=sentence[:160], due_at=due_at, due_hint=hint))
            if len(found) >= MAX_ASKS:
                return tuple(found)
    return tuple(found)


def _next_weekday(weekday: int, now: datetime, *, next_week: bool) -> datetime:
    days = (weekday - now.weekday()) % 7
    if days == 0 and now.hour >= 18:
        days = 7
    if next_week:
        days = days + 7 if days else 7
    return now + timedelta(days=days)


def parse_due(text: str, *, now: datetime | None = None) -> tuple[float | None, str]:
    """Resolve an unambiguous due hint to a local timestamp.

    Only phrases whose meaning is certain are resolved.  Anything else returns
    ``(None, hint)`` so the caller can ask exactly one clarifying question
    instead of inventing a date (FR-T2).
    """
    body = str(text or "").lower()
    moment = now or datetime.now()
    hint = ""
    target: datetime | None = None

    if match := re.search(r"\b(?:by|before|on|due)\s+(\d{1,2})(?:st|nd|rd|th)?\s+"
                          r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", body):
        hint = match.group(0)
        day = int(match.group(1))
        month = (
            "jan feb mar apr may jun jul aug sep oct nov dec".split().index(match.group(2))
            + 1
        )
        year = moment.year
        candidate = datetime(year, month, min(day, 28), 9, 0)
        if candidate.date() < moment.date():
            candidate = datetime(year + 1, month, min(day, 28), 9, 0)
        target = candidate
    elif match := re.search(r"\b(?:by|before|on)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", body):
        hint = match.group(0)
        target = _next_weekday(_WEEKDAYS[match.group(1)], moment, next_week=False).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
    elif re.search(r"\btomorrow\b", body):
        hint = "tomorrow"
        target = (moment + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif re.search(r"\b(?:today|end of day|eod)\b", body):
        hint = "today"
        target = moment.replace(hour=18, minute=0, second=0, microsecond=0)
    elif re.search(r"\bnext week\b", body):
        hint = "next week"
        target = (moment + timedelta(days=7)).replace(hour=9, minute=0, second=0, microsecond=0)

    if target is None:
        return None, hint
    return target.timestamp(), hint
