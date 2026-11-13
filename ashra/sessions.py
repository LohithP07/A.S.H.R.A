"""In-memory per-session conversation history.

This is deliberately small and ephemeral for the first brain implementation.
When the project grows into a personal assistant, replace SessionStore with a
persistent memory backend behind the same methods.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class SessionBackend(Protocol):
    """The small surface ChatService needs from a conversation store."""

    max_messages: int

    def ensure(self, session_id: str | None = None) -> str:
        ...

    def history(self, session_id: str) -> list[dict[str, str]]:
        ...

    def append_exchange(self, session_id: str, user: str, assistant: str) -> None:
        ...


@dataclass
class Message:
    role: str
    content: str
    created: float = field(default_factory=time.time)


@dataclass
class Session:
    id: str
    messages: list[Message] = field(default_factory=list)
    updated: float = field(default_factory=time.time)


class SessionStore:
    """Thread-safe, TTL-pruned conversation store."""

    def __init__(self, max_messages: int = 8, ttl_seconds: int = 6 * 60 * 60) -> None:
        self.max_messages = max(2, int(max_messages))
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.updated > self.ttl_seconds
        ]
        for session_id in stale:
            self._sessions.pop(session_id, None)

    def ensure(self, session_id: str | None = None) -> str:
        """Return a valid session id, creating a session if needed."""
        now = time.time()
        with self._lock:
            self._prune(now)
            if session_id and SESSION_RE.match(session_id):
                session = self._sessions.get(session_id)
                if session is None:
                    session = Session(id=session_id, updated=now)
                    self._sessions[session_id] = session
                session.updated = now
                return session_id

            new_id = uuid.uuid4().hex
            self._sessions[new_id] = Session(id=new_id, updated=now)
            return new_id

    def history(self, session_id: str) -> list[dict[str, str]]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return []
            return [
                {"role": message.role, "content": message.content}
                for message in session.messages
            ]

    def append_exchange(self, session_id: str, user: str, assistant: str) -> None:
        """Add one completed turn and trim to the configured short history."""
        now = time.time()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.messages.extend(
                [
                    Message("user", user, now),
                    Message("assistant", assistant, now),
                ]
            )
            if len(session.messages) > self.max_messages:
                session.messages = session.messages[-self.max_messages :]
            session.updated = now
