"""The ``spawn_agent`` tool exposed to the orchestrator."""

from __future__ import annotations

from typing import Any

from ..tools import Permission, ToolSpec
from .context import current_permission, current_request_id
from .registry import AgentRegistry
from .runner import SubagentRunner
from .spec import SPAWN_TOOL_NAME


def build_spawn_tool(
    runner: SubagentRunner,
    agents: AgentRegistry,
) -> ToolSpec:
    """Build the delegation tool from the current specialist registry."""
    names = agents.names()
    if not names:
        raise ValueError("Cannot build spawn_agent without at least one agent.")

    def handler(arguments: dict[str, Any]) -> Any:
        spec = agents.get(str(arguments.get("agent", "")))
        result = runner.run(
            spec,
            str(arguments.get("task", "")),
            granted=current_permission.get(),
            request_id=current_request_id.get(),
        )
        return result.as_dict()

    description = (
        "Delegate one scoped task to a specialist subagent. Available agents: "
        f"{', '.join(names)}. The subagent cannot exceed the caller's permission "
        "and cannot approve state-changing actions; it returns a structured "
        "summary with its answer, steps, and tools."
    )

    return ToolSpec(
        name=SPAWN_TOOL_NAME,
        description=description,
        handler=handler,
        permission=Permission.READ,
        input_schema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": names},
                "task": {"type": "string", "minLength": 3, "maxLength": 2000},
            },
            "required": ["agent", "task"],
            "additionalProperties": False,
        },
        tags=("agents", "delegate"),
    )
