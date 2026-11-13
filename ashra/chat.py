"""The note-grounded chat brain.

This module owns the answer pipeline:
    question -> session history -> keyword retrieval -> system prompt ->
    provider -> short conversation history -> JSON-ready result

``ChatService.ask`` returns one complete answer.  ``ChatService.stream`` walks
the same pipeline and yields small events so the viewer can paint the answer as
it arrives.  Keeping both paths here means retrieval, safety checks, and
persistence cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, load_config
from .errors import NotConfiguredError
from .notes import Note, load_notes
from .persona import compose_system_prompt, small_talk_reply
from .providers import ChatProvider, make_provider
from .recall import recall_reply
from .retrieval import ScoredNote, score_notes
from .sessions import SessionBackend, SessionStore


@dataclass(frozen=True)
class ChatResult:
    answer: str
    nodes: list[int]
    sources: list[dict[str, object]]
    session_id: str
    model: str


@dataclass(frozen=True)
class Prepared:
    """Everything retrieval and configuration decided before the model runs."""

    question: str
    session_id: str
    config: AppConfig
    history: list[dict[str, str]]
    matches: list[ScoredNote]
    immediate: ChatResult | None = None


# Backwards-compatible alias for the original private name.
_Prepared = Prepared


class ChatService:
    def __init__(
        self,
        base_dir: Path,
        sessions: SessionBackend | None = None,
        notes_loader: Callable[[], list[Note]] = load_notes,
        provider_factory: Callable[[AppConfig], ChatProvider] = make_provider,
    ) -> None:
        self.base_dir = base_dir
        self.config_path = base_dir / "config.json"
        self.sessions = sessions or SessionStore()
        self.notes_loader = notes_loader
        self.provider_factory = provider_factory

    # ------------------------------------------------------------------
    # shared pipeline
    # ------------------------------------------------------------------
    def prepare(self, question: str, session_id: str | None = None) -> Prepared:
        """Run retrieval and configuration without calling the model.

        The orchestrator uses this so tool-calling and plain answers share one
        grounding path.
        """
        return self._prepare(question, session_id)

    def _prepare(self, question: str, session_id: str | None) -> Prepared:
        question = " ".join(question.split()).strip()
        if not question:
            raise ValueError("Ask a question first.")
        if len(question) > 4000:
            raise ValueError("That question is too long. Keep it under 4,000 characters.")

        config = load_config(self.config_path)
        session_id = self.sessions.ensure(session_id)

        notes = self.notes_loader()
        recall = recall_reply(notes, question)
        if recall is not None:
            # A capture-history question has an exact answer in the vault, so
            # it bypasses retrieval and the provider entirely.
            self.sessions.append_exchange(session_id, question, recall.answer)
            return _Prepared(
                question=question,
                session_id=session_id,
                config=config,
                history=[],
                matches=[],
                immediate=ChatResult(
                    answer=recall.answer,
                    nodes=recall.nodes,
                    sources=recall.captures,
                    session_id=session_id,
                    model=config.model,
                ),
            )

        reply = small_talk_reply(question)
        if reply is not None:
            # Small talk reuses the session id but never enters the note history,
            # so a greeting cannot pollute later retrieval. It also works before
            # a provider key is configured, because it never calls the model.
            return _Prepared(
                question=question,
                session_id=session_id,
                config=config,
                history=[],
                matches=[],
                immediate=ChatResult(
                    answer=reply,
                    nodes=[],
                    sources=[],
                    session_id=session_id,
                    model=config.model,
                ),
            )

        if not config.configured:
            raise NotConfiguredError(
                "A.S.H.R.A is not configured. Paste your provider API key into "
                "config.json, set the model, then reload."
            )
        if config.provider not in {"anthropic", "nvidia", "openai_compatible"}:
            raise NotConfiguredError(
                f"Provider '{config.provider}' is not implemented yet. "
                "Use 'anthropic', 'nvidia', or 'openai_compatible'."
            )

        self.sessions.max_messages = max(2, config.max_history)
        history = self.sessions.history(session_id)
        previous_questions = [
            message["content"]
            for message in history
            if message["role"] == "user"
        ]

        matches = score_notes(
            notes,
            question,
            history=previous_questions,
            limit=config.max_notes,
        )

        return _Prepared(
            question=question,
            session_id=session_id,
            config=config,
            history=history,
            matches=matches,
        )

    @staticmethod
    def _sources(matches: list[ScoredNote]) -> list[dict[str, object]]:
        return [
            {
                "id": item.note.id,
                "label": item.note.label,
                "title": item.note.title,
                "path": item.note.path,
                "score": item.score,
            }
            for item in matches
        ]

    # ------------------------------------------------------------------
    # complete answer
    # ------------------------------------------------------------------
    def ask(self, question: str, session_id: str | None = None) -> ChatResult:
        prepared = self._prepare(question, session_id)
        if prepared.immediate is not None:
            return prepared.immediate

        if not prepared.matches:
            answer = "I don't have a note that covers that."
            self.sessions.append_exchange(prepared.session_id, prepared.question, answer)
            return ChatResult(
                answer=answer,
                nodes=[],
                sources=[],
                session_id=prepared.session_id,
                model=prepared.config.model,
            )

        system = build_system_prompt(prepared.matches, prepared.config)
        messages = prepared.history + [
            {"role": "user", "content": prepared.question}
        ]
        provider = self.provider_factory(prepared.config)
        answer = provider.complete(system, messages)
        self.sessions.append_exchange(prepared.session_id, prepared.question, answer)

        return ChatResult(
            answer=answer,
            nodes=[item.note.id for item in prepared.matches],
            sources=self._sources(prepared.matches),
            session_id=prepared.session_id,
            model=prepared.config.model,
        )

    # ------------------------------------------------------------------
    # streaming answer
    # ------------------------------------------------------------------
    def stream(
        self,
        question: str,
        session_id: str | None = None,
    ) -> Iterator[dict[str, object]]:
        prepared = self._prepare(question, session_id)
        yield {
            "event": "start",
            "session_id": prepared.session_id,
            "model": prepared.config.model,
        }

        if prepared.immediate is not None:
            yield from self._stream_result(prepared.immediate)
            return

        if not prepared.matches:
            answer = "I don't have a note that covers that."
            self.sessions.append_exchange(prepared.session_id, prepared.question, answer)
            yield from self._stream_result(
                ChatResult(
                    answer=answer,
                    nodes=[],
                    sources=[],
                    session_id=prepared.session_id,
                    model=prepared.config.model,
                )
            )
            return

        sources = self._sources(prepared.matches)
        nodes = [item.note.id for item in prepared.matches]
        yield {"event": "sources", "sources": sources, "nodes": nodes}

        system = build_system_prompt(prepared.matches, prepared.config)
        messages = prepared.history + [
            {"role": "user", "content": prepared.question}
        ]
        provider = self.provider_factory(prepared.config)
        answer = provider.complete(system, messages)
        self.sessions.append_exchange(prepared.session_id, prepared.question, answer)

        for chunk in _chunks(answer):
            yield {"event": "token", "text": chunk}

        yield {
            "event": "done",
            "answer": answer,
            "nodes": nodes,
            "sources": sources,
            "session_id": prepared.session_id,
            "model": prepared.config.model,
        }

    def _stream_result(self, result: ChatResult) -> Iterator[dict[str, object]]:
        yield {
            "event": "sources",
            "sources": result.sources,
            "nodes": result.nodes,
        }
        for chunk in _chunks(result.answer):
            yield {"event": "token", "text": chunk}
        yield {
            "event": "done",
            "answer": result.answer,
            "nodes": result.nodes,
            "sources": result.sources,
            "session_id": result.session_id,
            "model": result.model,
        }


def _chunks(text: str) -> Iterator[str]:
    """Split an answer into word-sized SSE chunks without losing whitespace."""
    for match in re.finditer(r"\S+\s*", text):
        yield match.group(0)


def build_system_prompt(matches: list[ScoredNote], config: AppConfig) -> str:
    blocks: list[str] = []

    for index, item in enumerate(matches, start=1):
        note = item.note
        text = note.text.strip()
        if len(text) > config.note_chars:
            text = text[: config.note_chars].rsplit(" ", 1)[0] + "…"
        blocks.append(f"[{index}] {note.title} ({note.path})")
        blocks.append(text)
        blocks.append("")

    return compose_system_prompt("\n".join(blocks))
