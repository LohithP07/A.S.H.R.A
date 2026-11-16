"""Shared fixtures for the email tests: a strict fake IMAP and message builders.

The fake is deliberately hostile about authority: any verb outside the
read-only set raises immediately, so a future change that quietly marks a
message read fails a test rather than a mailbox (FR-E8).
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr, formatdate

from ashra.connectors.gmail import FORBIDDEN_VERBS


def build_message(
    *,
    subject: str = "Wholesale pricing",
    body: str = "Could you send the wholesale price list by Tuesday?\n",
    sender: tuple[str, str] = ("Sarah Patel", "sarah@example.com"),
    to: str = "owner@example.com",
    message_id: str = "<m1@example.com>",
    references: tuple[str, ...] = (),
    attachment: str | None = None,
    list_id: str | None = None,
    cc: str | None = None,
) -> bytes:
    """Build one RFC 822 message as bytes."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(sender)
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Date"] = formatdate(1_760_000_000, usegmt=True)
    message["Message-ID"] = message_id
    if references:
        message["References"] = " ".join(references)
        message["In-Reply-To"] = references[-1]
    if list_id:
        message["List-Id"] = list_id
        message["List-Unsubscribe"] = "<mailto:unsubscribe@example.com>"
    message.set_content(body)
    if attachment:
        message.add_attachment(
            b"%PDF-1.4 fake attachment body",
            maintype="application",
            subtype="pdf",
            filename=attachment,
        )
    return message.as_bytes()


class FakeImap:
    """A read-only IMAP server with UID semantics and call recording."""

    def __init__(
        self,
        mailboxes: dict[str, list[tuple[int, bytes]]],
        *,
        uidvalidity: int = 7,
        missing_folders: set[str] | None = None,
    ) -> None:
        # The lists are shared, not copied, so a test can append a message
        # between two polls and the next connection sees it.
        self.mailboxes = dict(mailboxes)
        self.uidvalidity = uidvalidity
        self.missing = set(missing_folders or ())
        self.calls: list[tuple[object, ...]] = []
        self.selected: str | None = None
        self.readonly: bool | None = None
        self.password: str | None = None

    # -- connection ---------------------------------------------------
    def login(self, user: str, password: str):
        self.calls.append(("LOGIN", user))
        self.password = password
        return "OK", [b"logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        self.calls.append(("SELECT", mailbox, readonly))
        if mailbox in self.missing or mailbox not in self.mailboxes:
            return "NO", [b"no such mailbox"]
        self.selected = mailbox
        self.readonly = readonly
        return "OK", [str(len(self.mailboxes[mailbox])).encode()]

    def response(self, code: str):
        self.calls.append(("RESPONSE", code))
        if str(code).upper() == "UIDVALIDITY":
            return "UIDVALIDITY", [str(self.uidvalidity).encode()]
        return "OK", [b""]

    # -- read path ----------------------------------------------------
    def uid(self, command: str, *args):
        verb = str(command).upper()
        self.calls.append(("UID", verb, *args))
        if verb in FORBIDDEN_VERBS:  # pragma: no cover - the point of the fake
            raise AssertionError(f"The connector issued a write verb: {verb}")
        if verb == "SEARCH":
            return self._search(str(args[-1]))
        if verb == "FETCH":
            return self._fetch(int(args[0]), str(args[-1]))
        raise AssertionError(f"Unexpected UID command: {verb}")

    def _search(self, spec: str):
        # spec looks like "UID 1:*"
        token = spec.split()[-1]
        start = int(token.split(":")[0])
        if self.selected is None:
            return "NO", [b"no mailbox selected"]
        uids = [uid for uid, _raw in self.mailboxes[self.selected] if uid >= start]
        return "OK", [b" ".join(str(uid).encode() for uid in uids)]

    def _fetch(self, uid: int, spec: str):
        assert "BODY.PEEK[]" in spec, "A body fetch must use BODY.PEEK"
        if self.selected is None:
            return "NO", [b"no mailbox selected"]
        for candidate, raw in self.mailboxes[self.selected]:
            if candidate == uid:
                return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"]
        return "OK", [None]

    def close(self):
        self.calls.append(("CLOSE",))
        return "OK", [b"closed"]

    def logout(self):
        self.calls.append(("LOGOUT",))
        return "BYE", [b"bye"]

    # -- assertions used by tests -------------------------------------
    @property
    def verbs(self) -> list[str]:
        found: list[str] = []
        for call in self.calls:
            if call[0] == "UID":
                found.append(str(call[1]))
            else:
                found.append(str(call[0]))
        return found


def factory_for(instances: list[FakeImap], **kwargs):
    """Return a client factory that records every connection it opens."""

    def build(_host: str, _port: int, _timeout: float) -> FakeImap:
        client = FakeImap(**kwargs)
        instances.append(client)
        return client

    return build
