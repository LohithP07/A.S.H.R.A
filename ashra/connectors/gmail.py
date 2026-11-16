"""Gmail over IMAP, read-only (Phase 2E.1, PRD section 7.2).

The adapter is deliberately dull.  It opens a TLS connection, selects a folder
read-only, fetches unseen-by-cursor messages with ``BODY.PEEK[]``, hands the
bytes to :mod:`ashra.mailparse`, and advances a per-folder UID cursor.  It never
issues ``STORE``, ``APPEND``, ``EXPUNGE``, ``COPY``, or ``MOVE``: read-only is a
property of the code path, and the tests prove it by failing the fake client if
one of those verbs is ever called.

Two boundaries matter more than the IMAP details:

* the password is a callable, resolved from the credential vault at sync time,
  so a value is never held on the object between requests;
* ``normalise`` turns a message into a Source plus *candidates*.  Nothing here
  writes a task, a memory, or an external change.
"""

from __future__ import annotations

import imaplib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .. import sensitivity as privacy
from .base import (
    Candidate,
    Capability,
    Capabilities,
    Connector,
    NormalisedRecord,
    PollResult,
    Record,
    Source,
)
from ..errors import ConnectorError
from ..mailparse import (
    REPLY_NEEDED,
    ParsedMessage,
    body_hash,
    classify,
    extract_asks,
    parse_message,
)
from ..untrusted import scan

DEFAULT_HOST = "imap.gmail.com"
DEFAULT_PORT = 993
FORBIDDEN_VERBS = ("STORE", "APPEND", "EXPUNGE", "COPY", "MOVE", "DELETE")


class CursorStore(Protocol):
    """Where per-folder UID cursors live.  The database implements this."""

    def get_cursor(self, connector_id: str, folder: str) -> dict[str, Any] | None: ...

    def set_cursor(
        self,
        connector_id: str,
        folder: str,
        *,
        uidvalidity: int | None,
        uid: int,
    ) -> None: ...


class ImapClient(Protocol):
    """The tiny slice of ``imaplib`` this adapter is allowed to use."""

    def login(self, user: str, password: str) -> Any: ...

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[Any]]: ...

    def response(self, code: str) -> tuple[str, list[Any]]: ...

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]: ...

    def close(self) -> Any: ...

    def logout(self) -> Any: ...


def default_client_factory(host: str, port: int, timeout: float) -> ImapClient:
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


class MemoryCursorStore:
    """An in-process cursor store for tests and dry runs."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def get_cursor(self, connector_id: str, folder: str) -> dict[str, Any] | None:
        row = self._rows.get((connector_id, folder))
        return dict(row) if row else None

    def set_cursor(
        self,
        connector_id: str,
        folder: str,
        *,
        uidvalidity: int | None,
        uid: int,
    ) -> None:
        self._rows[(connector_id, folder)] = {
            "uidvalidity": uidvalidity,
            "uid": int(uid),
        }


def _int_or_none(value: Any) -> int | None:
    """Parse an IMAP integer that may arrive as ``bytes`` or ``str``.

    ``imaplib`` returns wire data as bytes, and ``str(b"7")`` is ``"b'7'"``:
    coercing without decoding silently turns every UID into ``None``.
    """
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("ascii", errors="ignore")
        except Exception:  # noqa: BLE001 - a malformed token is simply not a number
            return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _fetch_payload(data: Any) -> bytes | None:
    """Pull the raw message out of an imaplib FETCH response."""
    if not isinstance(data, (list, tuple)):
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None


class GmailConnector(Connector):
    """Read-only Gmail adapter with an injectable transport and cursor store."""

    def __init__(
        self,
        *,
        username: str,
        secret_provider: Callable[[], str],
        folders: tuple[str, ...] = ("INBOX",),
        credential_ref: str = "ashra/gmail",
        cursor_store: CursorStore | None = None,
        client_factory: Callable[[str, int, float], ImapClient] | None = None,
        classifier: Callable[[ParsedMessage], Any] | None = None,
        max_per_poll: int = 50,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = 30.0,
        min_interval_seconds: float = 30.0,
    ) -> None:
        super().__init__(
            connector_id="gmail",
            label="Gmail (read-only)",
            capabilities=Capabilities(read=True, draft=False, write=False, delete=False),
            scopes={"folders": list(folders), "mode": "read"},
            credential_ref=credential_ref,
            min_interval_seconds=min_interval_seconds,
        )
        self.username = str(username or "").strip()
        self.folders = tuple(folders)
        self._secret_provider = secret_provider
        self._cursor_store = cursor_store
        self._client_factory = client_factory or default_client_factory
        self._classify = classifier or classify
        self.max_per_poll = max(1, int(max_per_poll))
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._bodies: dict[str, ParsedMessage] = {}
        self.limitations: list[str] = []

    # ------------------------------------------------------------------
    # adapter surface
    # ------------------------------------------------------------------
    def poll(self, cursor: str | None = None) -> PollResult:
        self._bodies.clear()
        self.limitations.clear()
        if not self.username:
            raise ConnectorError(
                "Gmail is not configured: set connectors.gmail.username in config.json."
            )
        try:
            password = self._secret_provider()
        except Exception as exc:  # noqa: BLE001 - the vault explains by name
            raise ConnectorError(str(exc)) from None
        if not password:
            raise ConnectorError(
                f"Credential '{self.credential_ref}' is not in the vault. "
                "Add it with: python -m ashra.credentials set ashra/gmail"
            )

        client: ImapClient | None = None
        items: list[Record] = []
        skipped = 0
        exhausted = True
        cursors: dict[str, dict[str, Any]] = {}
        try:
            client = self._client_factory(self.host, self.port, self.timeout)
            client.login(self.username, password)
            for folder in self.folders:
                records, folder_exhausted, folder_skipped, cursor_row = self._poll_folder(
                    client, folder, cursor
                )
                items.extend(records)
                skipped += folder_skipped
                exhausted = exhausted and folder_exhausted
                cursors[folder] = cursor_row
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface a named, redacted failure
            raise ConnectorError(f"Gmail sync failed: {type(exc).__name__}: {exc}") from None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - a failed close must not mask a sync result
                    pass
                try:
                    client.logout()
                except Exception:  # noqa: BLE001
                    pass

        return PollResult(
            items=items,
            cursor=json.dumps(cursors, ensure_ascii=False),
            fetched=len(items),
            skipped=skipped,
            exhausted=exhausted,
        )

    def normalise(self, record: Record) -> NormalisedRecord | None:
        parsed = ParsedMessage.from_dict(dict(record.payload.get("parsed") or {}))
        folder = str(record.payload.get("folder") or "")
        classification = self._safe_classify(parsed)
        risk = scan(parsed.body)

        sensitivity = "sensitive" if parsed.attachments else "private"
        sensitivity = privacy.highest(sensitivity, privacy.classify(parsed.body))

        self._bodies[record.external_id] = parsed
        metadata: dict[str, Any] = {
            "folder": folder,
            "uid": record.payload.get("uid"),
            "from": parsed.from_addr,
            "to": list(parsed.to_addrs),
            "classification": classification.as_dict(),
            "inherited_from": "gmail",
            "body_hash": body_hash(parsed.body),
            "body_chars": parsed.body_chars,
            "attachments": [item.as_dict() for item in parsed.attachments],
            "attachment_count": len(parsed.attachments),
            "injection": risk.as_dict(),
            "model": "rules" if self._classify is classify else "local",
        }

        source = Source(
            connector_id=self.id,
            kind="email",
            external_id=record.external_id,
            thread_id=record.thread_id,
            subject=parsed.subject,
            occurred_at=parsed.occurred_at,
            sensitivity=sensitivity,
            metadata=metadata,
        )

        return NormalisedRecord(
            source=source,
            candidates=self._candidates(parsed, classification, sensitivity),
        )

    # ------------------------------------------------------------------
    # bodies (kept out of the registry contract; the mailbox stores them)
    # ------------------------------------------------------------------
    def body_for(self, external_id: str) -> ParsedMessage | None:
        return self._bodies.get(str(external_id))

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _safe_classify(self, parsed: ParsedMessage) -> Any:
        try:
            return self._classify(parsed)
        except Exception as exc:  # noqa: BLE001 - the model must never stop a sync
            # A model failure is a limitation, not an unhealthy mailbox: the
            # rules still classified the message and the sync is valid.
            self.limitations.append(f"classification fell back to rules: {exc}")
            return classify(parsed)

    def _candidates(
        self,
        parsed: ParsedMessage,
        classification: Any,
        sensitivity: str,
    ) -> tuple[Candidate, ...]:
        if sensitivity == "secret":
            # Secret-class content is never automated (PRD 6.1, FR-P3).  The
            # message is cached locally and shown, but it cannot become work.
            self.limitations.append("a secret-class message produced no proposal")
            return ()
        label = getattr(classification, "label", "")
        confidence = float(getattr(classification, "confidence", 0.5) or 0.5)
        if label != REPLY_NEEDED:
            return ()
        asks = extract_asks(parsed.body)
        if asks:
            return tuple(
                Candidate(
                    kind="task",
                    title=ask.text,
                    detail=parsed.subject,
                    due_at=ask.due_at,
                    person=parsed.sender or parsed.from_addr,
                    confidence=min(0.9, max(0.4, confidence)),
                    sensitivity=sensitivity,
                )
                for ask in asks
            )
        # A reply is needed but no clean ask was found: propose the reply itself
        # rather than inventing a task the owner never asked for.
        return (
            Candidate(
                kind="task",
                title=f"Reply to {parsed.sender or parsed.from_addr}: {parsed.subject}"[:160],
                detail=parsed.subject,
                person=parsed.sender or parsed.from_addr,
                confidence=min(0.7, max(0.35, confidence - 0.15)),
                sensitivity=sensitivity,
            ),
        )

    def _poll_folder(
        self,
        client: ImapClient,
        folder: str,
        cursor: str | None,
    ) -> tuple[list[Record], bool, int, dict[str, Any]]:
        # read-only select: the server refuses flags, and BODY.PEEK means no
        # message is marked read (FR-E2, FR-E8).
        typ, _data = client.select(folder, readonly=True)
        if typ != "OK":
            raise ConnectorError(
                f"Gmail folder '{folder}' is not available read-only. "
                "Check connectors.gmail.folders in config.json."
            )
        uidvalidity = self._uidvalidity(client)
        stored = self._stored_cursor(folder, cursor)
        start = 1
        if stored and stored.get("uidvalidity") in (None, uidvalidity):
            start = max(1, int(stored.get("uid") or 0) + 1)
        elif stored:
            # A changed UIDVALIDITY means the server renumbered the mailbox.
            # Re-read from the start; sources are idempotent by external id.
            start = 1

        uids = self._search(client, folder, start)
        batch = uids[: self.max_per_poll]
        records: list[Record] = []
        skipped = 0
        last_uid = start - 1
        for uid in batch:
            raw = self._fetch(client, uid)
            if raw is None:
                skipped += 1
                last_uid = uid
                continue
            try:
                parsed = parse_message(raw)
            except Exception:  # noqa: BLE001 - one bad message must not stop a folder
                skipped += 1
                last_uid = uid
                continue
            records.append(
                Record(
                    external_id=f"{folder}:{uidvalidity or 0}:{uid}",
                    kind="email",
                    subject=parsed.subject,
                    occurred_at=parsed.occurred_at,
                    thread_id=parsed.thread_id,
                    sensitivity="private",
                    payload={"parsed": parsed.as_dict(), "folder": folder, "uid": uid},
                )
            )
            last_uid = uid

        if len(batch) > len(records) and batch:
            last_uid = max(last_uid, batch[-1])
        if self._cursor_store is not None and batch:
            self._cursor_store.set_cursor(
                self.id, folder, uidvalidity=uidvalidity, uid=last_uid
            )
        return (
            records,
            len(uids) <= len(batch),
            skipped,
            {"uidvalidity": uidvalidity, "uid": last_uid},
        )

    def _stored_cursor(self, folder: str, cursor: str | None) -> dict[str, Any] | None:
        if self._cursor_store is not None:
            return self._cursor_store.get_cursor(self.id, folder)
        if not cursor:
            return None
        try:
            parsed = json.loads(cursor)
        except (TypeError, ValueError):
            return None
        row = parsed.get(folder) if isinstance(parsed, dict) else None
        return row if isinstance(row, dict) else None

    def _uidvalidity(self, client: ImapClient) -> int | None:
        try:
            typ, data = client.response("UIDVALIDITY")
        except Exception:  # noqa: BLE001 - some servers omit it; the cursor degrades
            return None
        if typ != "UIDVALIDITY" or not data:
            return None
        return _int_or_none(data[0])

    def _search(self, client: ImapClient, folder: str, start: int) -> list[int]:
        try:
            typ, data = client.uid("SEARCH", None, f"UID {start}:*")
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(
                f"Gmail search failed for '{folder}': {type(exc).__name__}"
            ) from None
        if typ != "OK" or not data:
            return []
        raw = data[0] or b""
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw)
        else:
            raw = str(raw).encode("ascii", errors="ignore")
        uids: list[int] = []
        for token in raw.split():
            value = _int_or_none(token)
            # RFC 3501: "n:*" always returns at least the highest UID, so a
            # value below the requested floor is noise and is dropped.
            if value is not None and value >= start:
                uids.append(value)
        return sorted(set(uids))

    def _fetch(self, client: ImapClient, uid: int) -> bytes | None:
        try:
            typ, data = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(
                f"Gmail fetch failed for UID {uid}: {type(exc).__name__}"
            ) from None
        if typ != "OK":
            return None
        return _fetch_payload(data)

    def status(self) -> dict[str, Any]:
        payload = super().status()
        payload["username"] = self.username or None
        payload["host"] = self.host
        payload["limitations"] = list(self.limitations)
        return payload


__all__ = [
    "Capability",
    "GmailConnector",
    "MemoryCursorStore",
    "default_client_factory",
    "FORBIDDEN_VERBS",
]
