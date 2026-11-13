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
from typing import Any

from .config import AppConfig, load_config
from .errors import NotConfiguredError
from .memory import MemoryStore, is_remember_request, remember_body
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
    proposals: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Prepared:
    """Everything retrieval and configuration decided before the model runs."""

    question: str
    session_id: str
    config: AppConfig
    history: list[dict[str, str]]
    matches: list[ScoredNote]
    memories: tuple[dict[str, Any], ...] = ()
    proposals: tuple[dict[str, Any], ...] = ()
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
        memory: MemoryStore | None = None,
    ) -> None:
        self.base_dir = base_dir
        self.config_path = base_dir / "config.json"
        self.sessions = sessions or SessionStore()
        self.notes_loader = notes_loader
        self.provider_factory = provider_factory
        self.memory = memory

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

        if self.memory is not None and is_remember_request(question):
            body = remember_body(question)
            remembered: list[dict[str, Any]] = []
            if not body:
                answer = "Tell me what to remember."
            else:
                try:
                    remembered = self.memory.remember_explicit(
                        body,
                        source="chat",
                        source_ref=session_id,
                    )
                except ValueError as exc:
                    answer = f"I couldn't store that in memory: {exc}"
                else:
                    answer = "Noted. I'll remember that."
            self.sessions.append_exchange(session_id, question, answer)
            return _Prepared(
                question=question,
                session_id=session_id,
                config=config,
                history=[],
                matches=[],
                immediate=ChatResult(
                    answer=answer,
                    nodes=[],
                    sources=[],
                    session_id=session_id,
                    model=config.model,
                    proposals=tuple(remembered),
                ),
            )

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

        proposals: tuple[dict[str, Any], ...] = ()
        if self.memory is not None:
            proposals = tuple(
                self.memory.propose_from_statement(
                    question,
                    source="chat",
                    source_ref=session_id,
                )
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

        memories: tuple[dict[str, Any], ...] = ()
        if self.memory is not None and config.max_memories > 0:
            memories = tuple(
                self.memory.search(question, limit=config.max_memories)
            )

        return _Prepared(
            question=question,
            session_id=session_id,
            config=config,
            history=history,
            matches=matches,
            memories=memories,
            proposals=proposals,
        )

    @staticmethod
    def _sources(
        matches: list[ScoredNote],
        memories: tuple[dict[str, Any], ...] = (),
    ) -> list[dict[str, object]]:
        sources: list[dict[str, object]] = [
            {
                "id": item.note.id,
                "label": item.note.label,
                "title": item.note.title,
                "path": item.note.path,
                "score": item.score,
            }
            for item in matches
        ]
        for memory in memories:
            sources.append(
                {
                    "id": memory.get("id"),
                    "label": str(memory.get("kind") or "memory").title(),
                    "title": memory.get("label") or memory.get("content"),
                    "path": memory.get("source_ref") or "",
                    "kind": "memory",
                    "score": memory.get("score"),
                }
            )
        return sources

    # ------------------------------------------------------------------
    # complete answer
    # ------------------------------------------------------------------
    def ask(self, question: str, session_id: str | None = None) -> ChatResult:
        prepared = self._prepare(question, session_id)
        if prepared.immediate is not None:
            return prepared.immediate

        if not prepared.matches and not prepared.memories:
            if prepared.proposals:
                answer = (
                    "I don't have a note or memory that covers that, but I've "
                    "proposed it as a memory for your approval."
                )
            else:
                answer = "I don't have a note or memory that covers that."
            self.sessions.append_exchange(prepared.session_id, prepared.question, answer)
            return ChatResult(
                answer=answer,
                nodes=[],
                sources=[],
                session_id=prepared.session_id,
                model=prepared.config.model,
                proposals=prepared.proposals,
            )

        system = build_system_prompt(
            prepared.matches,
            prepared.config,
            prepared.memories,
        )
        messages = prepared.history + [
            {"role": "user", "content": prepared.question}
        ]
        provider = self.provider_factory(prepared.config)
        answer = provider.complete(system, messages)
        self.sessions.append_exchange(prepared.session_id, prepared.question, answer)

        return ChatResult(
            answer=answer,
            nodes=[item.note.id for item in prepared.matches],
            sources=self._sources(prepared.matches, prepared.memories),
            session_id=prepared.session_id,
            model=prepared.config.model,
            proposals=prepared.proposals,
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

        if not prepared.matches and not prepared.memories:
            if prepared.proposals:
                answer = (
                    "I don't have a note or memory that covers that, but I've "
                    "proposed it as a memory for your approval."
                )
            else:
                answer = "I don't have a note or memory that covers that."
            self.sessions.append_exchange(prepared.session_id, prepared.question, answer)
            yield from self._stream_result(
                ChatResult(
                    answer=answer,
                    nodes=[],
                    sources=[],
                    session_id=prepared.session_id,
                    model=prepared.config.model,
                    proposals=prepared.proposals,
                )
            )
            return

        sources = self._sources(prepared.matches, prepared.memories)
        nodes = [item.note.id for item in prepared.matches]
        yield {"event": "sources", "sources": sources, "nodes": nodes}

        system = build_system_prompt(
            prepared.matches,
            prepared.config,
            prepared.memories,
        )
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
            "memories_proposed": [item.get("id") for item in prepared.proposals],
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
            "memories_proposed": [item.get("id") for item in result.proposals],
        }


def _chunks(text: str) -> Iterator[str]:
    """Split an answer into word-sized SSE chunks without losing whitespace."""
    for match in re.finditer(r"\S+\s*", text):
        yield match.group(0)


def build_system_prompt(
    matches: list[ScoredNote],
    config: AppConfig,
    memories: tuple[dict[str, Any], ...] = (),
) -> str:
    blocks: list[str] = []

    for index, item in enumerate(matches, start=1):
        note = item.note
        text = note.text.strip()
        if len(text) > config.note_chars:
            text = text[: config.note_chars].rsplit(" ", 1)[0] + "…"
        blocks.append(f"[{index}] {note.title} ({note.path})")
        blocks.append(text)
        blocks.append("")

    memory_blocks: list[str] = []
    for memory in memories:
        kind = str(memory.get("kind") or "memory")
        confidence = float(memory.get("confidence") or 0.0)
        source = str(memory.get("source") or "memory")
        content = str(memory.get("content") or "").strip()
        if not content:
            continue
        memory_blocks.append(
            f"- [{kind} · {confidence:.2f} · {source}] {content}"
        )

    return compose_system_prompt(
        "\n".join(blocks),
        "\n".join(memory_blocks),
    )
