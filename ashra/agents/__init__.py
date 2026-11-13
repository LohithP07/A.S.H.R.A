"""Scoped specialist subagents for A.S.H.R.A.

Phase 1E adds a second kind of worker.  The orchestrator stays the only entry
point: it may delegate one bounded task to a specialist, and that specialist
gets a *narrower* view of the world than its parent.

Three rules make that safe:

1. **Permission ceiling.** A child may never hold more permission than the
   parent granted.  ``operator`` asked for ``act`` under a ``read`` parent runs
   as ``read``.
2. **Tool allowlist.** Each agent declares the tools it may touch.  The runner
   also hides ``spawn_agent`` from children, so delegation cannot recurse.
3. **No approvals in children.** State-changing tools are never advertised to a
   child.  If a child asks for one anyway, the call is refused with an
   instruction to return the proposed action to the parent, which owns the
   approval queue.

Every run returns a structured summary and writes one ``agent`` audit event.
"""

from .context import current_permission, current_request_id, tool_context
from .registry import AgentRegistry, build_default_agents
from .runner import SubagentRunner, build_agent_system_prompt
from .spec import (
    MAX_AGENT_STEPS,
    SPAWN_TOOL_NAME,
    AgentResult,
    AgentSpec,
    lower_permission,
)
from .spawn import build_spawn_tool

__all__ = [
    "AgentRegistry",
    "AgentResult",
    "AgentSpec",
    "MAX_AGENT_STEPS",
    "SPAWN_TOOL_NAME",
    "SubagentRunner",
    "build_agent_system_prompt",
    "build_default_agents",
    "build_spawn_tool",
    "current_permission",
    "current_request_id",
    "lower_permission",
    "tool_context",
]
