"""Agent specifications and structured results.

An agent is a *scope*, not a second assistant.  It declares the highest
permission it may ever hold, which tools it may touch, and how many model turns
it may take.  The runner intersects that declaration with the parent's granted
permission, so a child can never widen its own authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from ..tools import Permission

SPAWN_TOOL_NAME = "spawn_agent"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_AGENT_STEPS = 4


def lower_permission(first: Permission, second: Permission) -> Permission:
    """Return the weaker of two permission tiers."""
    return first if first.rank <= second.rank else second


@dataclass(frozen=True)
class AgentSpec:
    """One specialist: instructions, a permission ceiling, and a tool scope."""

    name: str
    description: str
    instructions: str
    permission: Permission = Permission.READ
    tools: tuple[str, ...] = ("*",)
    max_steps: int = MAX_AGENT_STEPS

    def allows_tool(self, name: str) -> bool:
        """True when ``name`` matches one of this agent's tool patterns."""
        return any(fnmatchcase(str(name), pattern) for pattern in self.tools)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission.value,
            "tools": list(self.tools),
            "max_steps": self.max_steps,
        }


@dataclass(frozen=True)
class AgentResult:
    """The structured summary a subagent hands back to its parent."""

    agent: str
    status: str
    answer: str
    steps: int = 0
    tools: tuple[dict[str, Any], ...] = ()
    permission: Permission = Permission.READ

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "status": self.status,
            "answer": self.answer,
            "steps": self.steps,
            "tools": list(self.tools),
            "permission": self.permission.value,
        }
