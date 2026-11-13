"""The bounded tool-calling orchestrator.

The plain chat path answers one question in one model call.  This module adds
the loop that makes A.S.H.R.A useful: the model may ask for a tool, the
orchestrator runs it through :class:`ashra.tools.ToolRegistry`, feeds the result
back, and repeats until the model writes an answer.

Three rules keep that safe:

1. **Bounded.** At most ``max_steps`` model turns and a fixed number of tool
   calls per turn.  The loop can never run forever.
2. **Permissioned.** Every call goes through the registry's permission gate.
   Tools that change state also require explicit human approval; the pending
   call is persisted so the browser can approve or deny it later.
3. **Audited.** Tool calls are audited by the registry, and approvals are
   audited here, so every side effect has a trail.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .agents.context import tool_context
from .chat import ChatResult, ChatService, Prepared, _chunks, build_system_prompt
from .config import load_config
from .db import Database
from .errors import ToolNotFound
from .providers import ToolCall, ToolOutcome, ToolTurn
from .tools import Permission, ToolRegistry, ToolSpec, parse_permission

MAX_STEPS = 6
MAX_CALLS_PER_TURN = 6
MAX_EVENT_RESULT_CHARS = 4000


@dataclass(frozen=True)
class OrchestratorResult:
    answer: str
    nodes: list[int]
    sources: list[dict[str, Any]]
    session_id: str
    model: str
    steps: int = 0
    tools: tuple[dict[str, Any], ...] = ()
    pending: dict[str, Any] | None = None
    status: str = "ok"
    proposal_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "answer": self.answer,
            "nodes": self.nodes,
            "sources": self.sources,
            "session_id": self.session_id,
            "model": self.model,
            "steps": self.steps,
            "tools": list(self.tools),
        }
        if self.pending is not None:
            payload["pending_approval"] = self.pending
        if self.proposal_ids:
            payload["memories_proposed"] = list(self.proposal_ids)
        return payload


class Orchestrator:
    """Run the note-grounded answer through a permissioned tool loop."""

    def __init__(
        self,
        chat: ChatService,
        registry: ToolRegistry,
        database: Database | None = None,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.chat = chat
        self.registry = registry
        self.database = database
        self.max_steps = max(1, int(max_steps))

    # ------------------------------------------------------------------
    # public entry points
    # ------------------------------------------------------------------
    def stream(
        self,
        question: str,
        session_id: str | None = None,
        *,
        granted: Permission = Permission.READ,
        request_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        prepared = self.chat.prepare(question, session_id)
        yield {
            "event": "start",
            "session_id": prepared.session_id,
            "model": prepared.config.model,
        }

        if prepared.immediate is not None:
            yield from self.chat._stream_result(prepared.immediate)
            return

        if not prepared.matches and not prepared.memories:
            if prepared.proposals:
                answer = (
                    "I don't have a note or memory that covers that, but I've "
                    "proposed it as a memory for your approval."
                )
            else:
                answer = "I don't have a note or memory that covers that."
            self.chat.sessions.append_exchange(prepared.session_id, prepared.question, answer)
            yield from self.chat._stream_result(
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

        sources = self.chat._sources(prepared.matches, prepared.memories)
        nodes = [item.note.id for item in prepared.matches]
        yield {"event": "sources", "sources": sources, "nodes": nodes}

        system = build_system_prompt(
            prepared.matches,
            prepared.config,
            prepared.memories,
        )
        provider = self.chat.provider_factory(prepared.config)
        state = self._state(prepared, system, granted)

        if not hasattr(provider, "complete_turn"):
            # A provider without tool support still answers, just without tools.
            yield from self._plain(provider, state, system)
            return

        yield from self._loop(state, provider, system, request_id)

    def ask(
        self,
        question: str,
        session_id: str | None = None,
        *,
        granted: Permission = Permission.READ,
        request_id: str | None = None,
    ) -> OrchestratorResult:
        return self._collect(
            self.stream(
                question,
                session_id,
                granted=granted,
                request_id=request_id,
            )
        )

    def resume(
        self,
        approval_id: str,
        *,
        approve: bool,
        request_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        if self.database is None:
            raise ValueError("Approvals need a database.")
        self.database.expire_approvals()

        row = self.database.get_approval(approval_id)
        if row is None:
            raise ValueError("That approval does not exist.")
        if row["status"] != "pending":
            raise ValueError("That approval is no longer pending.")

        decided = self.database.decide_approval(approval_id, approve)
        if decided is None:
            raise ValueError("That approval was already decided.")
        self.database.record_audit(
            "approval",
            "decide",
            status="approved" if approve else "denied",
            permission=row.get("permission"),
            request_id=request_id or row.get("request_id"),
            session_id=row.get("session_id"),
            detail={"tool": row.get("tool"), "arguments": row.get("arguments")},
        )

        if not approve:
            answer = "Understood. I did not run that action."
            session_id = str(row.get("session_id") or "")
            question = str(row.get("question") or "")
            if session_id:
                self.chat.sessions.append_exchange(session_id, question, answer)
            self.database.complete_approval(approval_id, "denied", {"answer": answer})
            yield {
                "event": "start",
                "session_id": session_id,
                "model": str((row.get("state") or {}).get("model") or ""),
            }
            for chunk in _chunks(answer):
                yield {"event": "token", "text": chunk}
            yield {
                "event": "done",
                "status": "denied",
                "answer": answer,
                "nodes": [],
                "sources": [],
                "session_id": session_id,
                "model": str((row.get("state") or {}).get("model") or ""),
                "steps": 0,
                "tools": [],
            }
            return

        state = self._restore(row.get("state"))
        if state is None:
            raise ValueError("That approval has no saved conversation state.")
        state["approval_id"] = approval_id

        turn: ToolTurn | None = state.get("turn")
        if turn is None or state["next_call"] >= len(turn.calls):
            raise ValueError("That approval no longer matches a pending tool call.")
        call = turn.calls[state["next_call"]]
        if call.name != row.get("tool"):
            raise ValueError("That approval no longer matches a pending tool call.")

        config = load_config(self.chat.config_path)
        provider = self.chat.provider_factory(config)

        yield {
            "event": "start",
            "session_id": state["session_id"],
            "model": state["model"],
        }
        yield {
            "event": "tool_call",
            "call_id": call.id,
            "name": call.name,
            "arguments": call.arguments,
            "permission": row.get("permission"),
            "requires_approval": False,
            "approved": True,
        }
        outcome = self._execute(
            call,
            state["granted"],
            approved=True,
            request_id=request_id,
        )
        state["outcomes"].append(outcome)
        state["next_call"] += 1
        state["approved_calls"].add(call.id)
        self._log(state, outcome)
        yield self._tool_result_event(outcome)

        yield from self._loop(state, provider, state["system"], request_id)

    def resume_result(
        self,
        approval_id: str,
        *,
        approve: bool,
        request_id: str | None = None,
    ) -> OrchestratorResult:
        return self._collect(
            self.resume(approval_id, approve=approve, request_id=request_id)
        )

    # ------------------------------------------------------------------
    # state helpers
    # ------------------------------------------------------------------
    def _state(
        self,
        prepared: Prepared,
        system: str,
        granted: Permission,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = list(prepared.history) + [
            {"role": "user", "content": prepared.question}
        ]
        return {
            "question": prepared.question,
            "session_id": prepared.session_id,
            "model": prepared.config.model,
            "nodes": [item.note.id for item in prepared.matches],
            "sources": self.chat._sources(prepared.matches, prepared.memories),
            "messages": messages,
            "system": system,
            "step": 1,
            "turn": None,
            "outcomes": [],
            "next_call": 0,
            "granted": granted,
            "approved_calls": set(),
            "tool_log": [],
            "proposals": list(prepared.proposals),
            "approval_id": None,
        }

    @staticmethod
    def _outcome_state(outcome: ToolOutcome) -> dict[str, Any]:
        return {
            "call_id": outcome.call_id,
            "name": outcome.name,
            "ok": outcome.ok,
            "result": outcome.result,
            "error": outcome.error,
        }

    def _restore(self, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        turn_data = payload.get("turn")
        if not isinstance(turn_data, dict):
            return None
        calls = tuple(
            ToolCall(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                arguments=item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
            )
            for item in (turn_data.get("calls") or [])
            if isinstance(item, dict)
        )
        assistant_message = turn_data.get("assistant_message")
        if not isinstance(assistant_message, dict):
            assistant_message = {"role": "assistant", "content": ""}

        outcomes: list[ToolOutcome] = []
        for item in payload.get("outcomes") or []:
            if not isinstance(item, dict):
                continue
            outcomes.append(
                ToolOutcome(
                    call_id=str(item.get("call_id") or ""),
                    name=str(item.get("name") or ""),
                    ok=bool(item.get("ok")),
                    result=item.get("result"),
                    error=item.get("error"),
                )
            )

        return {
            "question": str(payload.get("question") or ""),
            "session_id": str(payload.get("session_id") or ""),
            "model": str(payload.get("model") or ""),
            "nodes": list(payload.get("nodes") or []),
            "sources": list(payload.get("sources") or []),
            "messages": list(payload.get("messages") or []),
            "system": str(payload.get("system") or ""),
            "step": int(payload.get("step") or 1),
            "turn": ToolTurn(
                text=str(turn_data.get("text") or ""),
                calls=calls,
                assistant_message=assistant_message,
            ),
            "outcomes": outcomes,
            "next_call": int(payload.get("next_call") or 0),
            "granted": parse_permission(payload.get("granted")),
            "approved_calls": set(payload.get("approved_calls") or []),
            "tool_log": list(payload.get("tool_log") or []),
            "proposals": list(payload.get("proposals") or []),
            "approval_id": None,
        }

    def _approval_state(
        self,
        state: dict[str, Any],
        turn: ToolTurn,
    ) -> dict[str, Any]:
        return {
            "question": state["question"],
            "session_id": state["session_id"],
            "model": state["model"],
            "nodes": state["nodes"],
            "sources": state["sources"],
            "messages": state["messages"],
            "system": state["system"],
            "step": state["step"],
            "turn": {
                "text": turn.text,
                "assistant_message": turn.assistant_message,
                "calls": [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in turn.calls
                ],
            },
            "outcomes": [self._outcome_state(item) for item in state["outcomes"]],
            "next_call": state["next_call"],
            "granted": state["granted"].value,
            "approved_calls": sorted(state["approved_calls"]),
            "tool_log": state["tool_log"],
            "proposals": state.get("proposals", []),
        }

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------
    def _loop(
        self,
        state: dict[str, Any],
        provider: Any,
        system: str,
        request_id: str | None,
    ) -> Iterator[dict[str, Any]]:
        while state["step"] <= self.max_steps:
            turn: ToolTurn | None = state.get("turn")
            if turn is None:
                turn = provider.complete_turn(
                    system,
                    state["messages"],
                    self._schemas(provider, state["granted"]),
                )
                state["turn"] = turn

                if not turn.calls:
                    yield from self._finish(state, turn.text)
                    return
                if len(turn.calls) > MAX_CALLS_PER_TURN:
                    yield from self._finish(
                        state,
                        "I wanted to run too many tools at once, so I stopped. "
                        "Ask me for one thing at a time.",
                    )
                    return

                state["messages"].append(turn.assistant_message)
                state["outcomes"] = []
                state["next_call"] = 0

            calls = turn.calls
            while state["next_call"] < len(calls):
                call = calls[state["next_call"]]
                spec = self._spec(call.name)
                requires_approval = bool(
                    spec
                    and spec.requires_approval
                    and state["granted"].allows(spec.permission)
                )

                if requires_approval and call.id not in state["approved_calls"]:
                    approval = self._save_approval(
                        state,
                        turn,
                        call,
                        spec,
                        request_id,
                    )
                    yield {
                        "event": "tool_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                        "permission": spec.permission.value if spec else None,
                        "requires_approval": True,
                    }
                    yield {
                        "event": "approval_required",
                        "approval": approval,
                    }
                    yield {
                        "event": "done",
                        "status": "awaiting_approval",
                        "answer": "",
                        "nodes": state["nodes"],
                        "sources": state["sources"],
                        "session_id": state["session_id"],
                        "model": state["model"],
                        "steps": state["step"],
                        "tools": list(state["tool_log"]),
                        "pending_approval": approval,
                    }
                    return

                yield {
                    "event": "tool_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "permission": spec.permission.value if spec else None,
                    "requires_approval": requires_approval,
                    "approved": call.id in state["approved_calls"],
                }
                outcome = self._execute(
                    call,
                    state["granted"],
                    approved=call.id in state["approved_calls"],
                    request_id=request_id,
                )
                state["outcomes"].append(outcome)
                state["next_call"] += 1
                self._log(state, outcome)
                yield self._tool_result_event(outcome)

            state["messages"].extend(
                provider.tool_messages(turn, state["outcomes"])
            )
            state["turn"] = None
            state["outcomes"] = []
            state["next_call"] = 0
            state["step"] += 1

        yield from self._finish(
            state,
            "I stopped after several tool steps without reaching a final answer. "
            "Try asking in a simpler or more specific way.",
        )

    def _plain(self, provider: Any, state: dict[str, Any], system: str) -> Iterator[dict[str, Any]]:
        answer = provider.complete(system, state["messages"])
        self.chat.sessions.append_exchange(state["session_id"], state["question"], answer)
        for chunk in _chunks(answer):
            yield {"event": "token", "text": chunk}
        yield self._done(state, answer, "ok")

    def _finish(
        self,
        state: dict[str, Any],
        answer: str,
        status: str = "ok",
    ) -> Iterator[dict[str, Any]]:
        self.chat.sessions.append_exchange(state["session_id"], state["question"], answer)
        for chunk in _chunks(answer):
            yield {"event": "token", "text": chunk}

        payload = self._done(state, answer, status)
        approval_id = state.get("approval_id")
        if approval_id and self.database is not None:
            self.database.complete_approval(approval_id, "completed", payload)
        yield payload

    # ------------------------------------------------------------------
    # tool execution
    # ------------------------------------------------------------------
    def _schemas(self, provider: Any, granted: Permission) -> list[dict[str, Any]]:
        style = getattr(provider, "tool_schema_style", "openai")
        return [spec.provider_schema(style) for spec in self.registry.specs(granted)]

    def _spec(self, name: str) -> ToolSpec | None:
        try:
            return self.registry.get(name)
        except ToolNotFound:
            return None

    def _execute(
        self,
        call: ToolCall,
        granted: Permission,
        *,
        approved: bool,
        request_id: str | None,
    ) -> ToolOutcome:
        try:
            with tool_context(granted, request_id):
                result = self.registry.invoke(
                    call.name,
                    call.arguments,
                    granted=granted,
                    approved=approved,
                    request_id=request_id,
                )
        except Exception as exc:  # noqa: BLE001 - fed back to the model as data
            return ToolOutcome(
                call_id=call.id,
                name=call.name,
                ok=False,
                error=str(exc)[:1000],
            )
        return ToolOutcome(call_id=call.id, name=call.name, ok=True, result=result)

    def _log(self, state: dict[str, Any], outcome: ToolOutcome) -> None:
        state["tool_log"].append(
            {
                "name": outcome.name,
                "ok": outcome.ok,
                "error": outcome.error,
            }
        )

    def _save_approval(
        self,
        state: dict[str, Any],
        turn: ToolTurn,
        call: ToolCall,
        spec: ToolSpec | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        if self.database is None:
            raise ValueError("Approvals need a database.")
        approval_id = self.database.create_approval(
            tool=call.name,
            arguments=call.arguments,
            permission=spec.permission.value if spec else None,
            session_id=state["session_id"],
            request_id=request_id,
            question=state["question"],
            state=self._approval_state(state, turn),
        )
        self.database.record_audit(
            "approval",
            "create",
            status="pending",
            permission=spec.permission.value if spec else None,
            request_id=request_id,
            session_id=state["session_id"],
            detail={"tool": call.name, "arguments": call.arguments, "approval_id": approval_id},
        )
        return {
            "id": approval_id,
            "status": "pending",
            "tool": call.name,
            "arguments": call.arguments,
            "permission": spec.permission.value if spec else None,
            "requires_approval": True,
        }

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    @staticmethod
    def _tool_result_event(outcome: ToolOutcome) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": "tool_result",
            "call_id": outcome.call_id,
            "name": outcome.name,
            "ok": outcome.ok,
        }
        if outcome.ok:
            payload["result"] = _preview(outcome.result)
        else:
            payload["error"] = outcome.error
        return payload

    @staticmethod
    def _done(state: dict[str, Any], answer: str, status: str) -> dict[str, Any]:
        return {
            "event": "done",
            "status": status,
            "answer": answer,
            "nodes": state["nodes"],
            "sources": state["sources"],
            "session_id": state["session_id"],
            "model": state["model"],
            "steps": state["step"],
            "tools": list(state["tool_log"]),
            "memories_proposed": [
                item.get("id")
                for item in state.get("proposals", [])
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _collect(events: Iterable[dict[str, Any]]) -> OrchestratorResult:
        done: dict[str, Any] = {}
        for event in events:
            if event.get("event") == "done":
                done = event
        return OrchestratorResult(
            answer=str(done.get("answer") or ""),
            nodes=list(done.get("nodes") or []),
            sources=list(done.get("sources") or []),
            session_id=str(done.get("session_id") or ""),
            model=str(done.get("model") or ""),
            steps=int(done.get("steps") or 0),
            tools=tuple(done.get("tools") or ()),
            pending=done.get("pending_approval"),
            status=str(done.get("status") or "ok"),
            proposal_ids=tuple(done.get("memories_proposed") or ()),
        )


def _preview(value: Any, limit: int = MAX_EVENT_RESULT_CHARS) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return value
