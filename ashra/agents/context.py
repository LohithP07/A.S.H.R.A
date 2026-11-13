"""Request-scoped context for nested tool calls.

A tool handler only receives its arguments, so the ``spawn_agent`` tool needs
another way to learn the caller's permission tier and request id.  These
ContextVars carry that context across the synchronous call stack without
changing the tool contract.

The API sets the context around ``POST /tools/invoke`` and the orchestrator sets
it around every tool execution.  A child runner narrows the permission before
invoking its own tools.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ..tools import Permission, parse_permission

current_permission: ContextVar[Permission] = ContextVar(
    "ashra_current_permission",
    default=Permission.READ,
)
current_request_id: ContextVar[str | None] = ContextVar(
    "ashra_current_request_id",
    default=None,
)


@contextmanager
def tool_context(
    granted: Permission | str = Permission.READ,
    request_id: str | None = None,
) -> Iterator[Permission]:
    """Temporarily expose the caller's permission and request id to tools."""
    permission = parse_permission(granted)
    permission_token = current_permission.set(permission)
    request_token = current_request_id.set(request_id)
    try:
        yield permission
    finally:
        current_request_id.reset(request_token)
        current_permission.reset(permission_token)
