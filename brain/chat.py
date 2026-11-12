"""The note-grounded chat brain.

This module owns the answer pipeline:
    question -> session history -> keyword retrieval -> system prompt ->
    provider -> short conversation history -> JSON-ready result
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, load_config
from .errors import NotConfiguredError
from .notes import Note, load_notes
from .persona import compose_system_prompt, small_talk_reply
from .providers import ChatProvider, make_provider
from .recall import recall_reply
from .retrieval import ScoredNote, score_notes
from .sessions import SessionStore


@dataclass(frozen=True)
class ChatResult:
    answer: str
    nodes: list[int]
    sources: list[dict[str, object]]
    session_id: str
    model: str


class ChatService:
    def __init__(
        self,
        base_dir: Path,
        sessions: SessionStore | None = None,
        notes_loader: Callable[[], list[Note]] = load_notes,
        provider_factory: Callable[[AppConfig], ChatProvider] = make_provider,
    ) -> None:
        self.base_dir = base_dir
        self.config_path = base_dir / "config.json"
        self.sessions = sessions or SessionStore()
        self.notes_loader = notes_loader
        self.provider_factory = provider_factory

    def ask(self, question: str, session_id: str | None = None) -> ChatResult:
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
            return ChatResult(
                answer=recall.answer,
                nodes=recall.nodes,
                sources=recall.captures,
                session_id=session_id,
                model=config.model,
            )

        if not config.configured:
            raise NotConfiguredError(
                "The brain is not configured. Paste your provider API key into "
                "config.json, set the model, then reload."
            )
        if config.provider not in {"anthropic", "nvidia", "openai_compatible"}:
            raise NotConfiguredError(
                f"Provider '{config.provider}' is not implemented yet. "
                "Use 'anthropic', 'nvidia', or 'openai_compatible'."
            )

        reply = small_talk_reply(question)
        if reply is not None:
            # Small talk reuses the session id but never enters the note history,
            # so a greeting cannot pollute later retrieval.
            return ChatResult(
                answer=reply,
                nodes=[],
                sources=[],
                session_id=session_id,
                model=config.model,
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

        if not matches:
            answer = "I don't have a note that covers that."
            self.sessions.append_exchange(session_id, question, answer)
            return ChatResult(
                answer=answer,
                nodes=[],
                sources=[],
                session_id=session_id,
                model=config.model,
            )

        system = build_system_prompt(matches, config)
        messages = history + [{"role": "user", "content": question}]
        provider = self.provider_factory(config)
        answer = provider.complete(system, messages)
        self.sessions.append_exchange(session_id, question, answer)

        sources = [
            {
                "id": item.note.id,
                "label": item.note.label,
                "title": item.note.title,
                "path": item.note.path,
                "score": item.score,
            }
            for item in matches
        ]
        return ChatResult(
            answer=answer,
            nodes=[item.note.id for item in matches],
            sources=sources,
            session_id=session_id,
            model=config.model,
        )


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
