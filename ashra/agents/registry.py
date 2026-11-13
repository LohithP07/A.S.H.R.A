"""The specialist registry and the default agent pack."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from ..errors import ToolNotFound
from ..tools import Permission
from .spec import NAME_RE, AgentSpec


class AgentRegistry:
    """Register specialists once and look them up by name."""

    def __init__(self, agents: Iterable[AgentSpec] | None = None) -> None:
        self._agents: dict[str, AgentSpec] = {}
        for spec in agents or ():
            self.register(spec)

    def register(self, spec: AgentSpec) -> AgentSpec:
        name = str(spec.name or "").strip()
        if not NAME_RE.match(name):
            raise ValueError(
                f"Agent name '{spec.name}' must use lowercase letters, digits, "
                "underscores, or hyphens (max 32 characters)."
            )
        if name in self._agents:
            raise ValueError(f"Agent '{name}' is already registered.")
        self._agents[name] = spec
        return spec

    def get(self, name: str) -> AgentSpec:
        key = str(name or "").strip()
        spec = self._agents.get(key)
        if spec is None:
            raise ToolNotFound(f"Agent '{name}' is not registered.")
        return spec

    def names(self) -> list[str]:
        return sorted(self._agents)

    def list(self) -> list[dict[str, object]]:
        return [self._agents[name].as_dict() for name in self.names()]

    def __len__(self) -> int:
        return len(self._agents)

    def __iter__(self) -> Iterator[AgentSpec]:
        for name in self.names():
            yield self._agents[name]


def build_default_agents() -> list[AgentSpec]:
    """The Phase 1E specialist pack.

    ``researcher`` and ``writer`` are read-oriented.  ``operator`` may hold
    ``act``, but every current state-changing tool is approval-gated, so it is
    expected to hand those proposals back to the parent until Phase 1F adds
    safe, non-approval actions.
    """
    return [
        AgentSpec(
            name="researcher",
            description="Read-only research across the notes vault and read-only integrations.",
            instructions=(
                "You are the researcher subagent. Search the notes vault and any "
                "connected read-only integrations, then report only what the tool "
                "results support. Name concrete facts, dates, people, and paths. "
                "Do not speculate. Return a compact brief and list the evidence "
                "you used."
            ),
            permission=Permission.READ,
            tools=("*",),
        ),
        AgentSpec(
            name="writer",
            description="Turns supplied material into a concise draft without sending it.",
            instructions=(
                "You are the writer subagent. Turn the supplied material into "
                "clear, concise prose in the user's voice. Read notes when you "
                "need context, but do not invent facts, do not send anything, "
                "and do not claim an action was taken. Return the draft text only."
            ),
            permission=Permission.DRAFT,
            tools=("fs_*", "*_search", "*_read"),
        ),
        AgentSpec(
            name="operator",
            description="Inspects state and prepares concrete actions for the parent.",
            instructions=(
                "You are the operator subagent. Inspect state and prepare concrete "
                "actions. You may run tools that are safe and do not require "
                "approval. If an action needs approval, stop and return the exact "
                "tool name and arguments to the parent instead of attempting it."
            ),
            permission=Permission.ACT,
            tools=("*",),
        ),
    ]
