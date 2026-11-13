"""Connect MCP servers and expose their read-only tools to A.S.H.R.A.

The manager is intentionally small: it probes each configured server once,
turns the advertised tools into ordinary :class:`~ashra.tools.ToolSpec`
entries, and records a per-server status.  A broken integration degrades to a
readable error instead of breaking chat or the other servers.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import IntegrationError
from ..tools import ToolRegistry, ToolSpec
from .config import IntegrationConfig
from .mcp_client import MCPConnection, RemoteTool, make_connection

MAX_TOOL_NAME = 64
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("_", str(value or "").lower()).strip("_")
    return slug or "integration"


def _tool_name(prefix: str, remote_name: str) -> str:
    """Namespace a remote tool and keep it inside provider name limits."""
    remote = _slug(remote_name)
    base = remote if remote.startswith(f"{prefix}_") else f"{prefix}_{remote}"
    if len(base) <= MAX_TOOL_NAME:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"{base[: MAX_TOOL_NAME - 9]}_{digest}"


def _handler(connection: MCPConnection, remote_name: str) -> Callable[[dict[str, Any]], Any]:
    def handler(arguments: dict[str, Any]) -> Any:
        return connection.call_tool(remote_name, dict(arguments or {}))

    return handler


def _description(config: IntegrationConfig, tool: RemoteTool) -> str:
    detail = tool.description or tool.name
    return f"[{config.name}] {detail} (read-only MCP integration)"


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:300]


@dataclass
class IntegrationState:
    """Everything known about one configured integration."""

    config: IntegrationConfig
    connection: MCPConnection | None = None
    tools: list[RemoteTool] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def connected(self) -> bool:
        return self.connection is not None and self.error is None


class IntegrationManager:
    """Probe MCP servers, register their tools, and report their health."""

    def __init__(
        self,
        integrations: list[IntegrationConfig] | None = None,
        *,
        connection_factory: Callable[[IntegrationConfig], MCPConnection] | None = None,
    ) -> None:
        self._configs = list(integrations or [])
        self._factory = connection_factory or make_connection
        self._states: dict[str, IntegrationState] = {}
        self._connected = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def connect_all(self) -> list[dict[str, Any]]:
        """Probe every enabled server.  Idempotent until ``close()``."""
        if self._connected:
            return self.status()

        self._states = {}
        for config in self._configs:
            state = IntegrationState(config)
            self._states[config.name] = state
            if not config.enabled:
                continue

            connection: MCPConnection | None = None
            try:
                connection = self._factory(config)
                tools = connection.list_tools()
                if not isinstance(tools, list):
                    raise IntegrationError(
                        f"Integration '{config.name}' returned an invalid tool list."
                    )
                state.connection = connection
                state.tools = tools
            except Exception as exc:  # noqa: BLE001 - reported as status data
                state.error = _short_error(exc)
                if connection is not None:
                    self._safe_close(connection)

        self._connected = True
        return self.status()

    def close(self) -> None:
        for state in self._states.values():
            if state.connection is not None:
                self._safe_close(state.connection)
                state.connection = None
        self._connected = False

    @staticmethod
    def _safe_close(connection: MCPConnection) -> None:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def register_tools(self, registry: ToolRegistry) -> list[dict[str, Any]]:
        """Register every connected server's tools under a name prefix."""
        for state in self._states.values():
            if not state.connected or state.connection is None:
                continue

            prefix = state.config.prefix
            for tool in state.tools:
                name = _tool_name(prefix, tool.name)
                if name in state.registered:
                    continue
                spec = ToolSpec(
                    name=name,
                    description=_description(state.config, tool),
                    handler=_handler(state.connection, tool.name),
                    permission=state.config.permission,
                    input_schema=tool.input_schema,
                    tags=("integration", state.config.name),
                )
                try:
                    registry.register(spec)
                except ValueError:
                    # A local tool already owns this name; keep the local one.
                    state.skipped.append(name)
                    continue
                state.registered.append(name)
        return self.status()

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for config in self._configs:
            state = self._states.get(config.name)
            result.append(
                {
                    "name": config.name,
                    "transport": config.transport,
                    "permission": config.permission.value,
                    "enabled": config.enabled,
                    "connected": bool(state and state.connected),
                    "tool_count": len(state.registered) if state else 0,
                    "tools": list(state.registered) if state else [],
                    "error": state.error if state else None,
                    "skipped": list(state.skipped) if state else [],
                }
            )
        return result

    def get(self, name: str) -> IntegrationState | None:
        return self._states.get(str(name or "").strip())

    @property
    def configured(self) -> int:
        return len(self._configs)
