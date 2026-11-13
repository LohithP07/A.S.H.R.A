"""Synchronous boundary around the optional MCP client SDK.

The MCP SDK is async and optional, while A.S.H.R.A's tool registry is
synchronous.  This module hides that mismatch behind two methods:

    connection.list_tools()             -> list[RemoteTool]
    connection.call_tool(name, args)    -> JSON-ready result

Each operation opens a short-lived MCP session.  That keeps the client
stateless, avoids a background event loop per server, and means a crashed
server cannot poison a long-lived session.  Read-only tools are cheap enough
for this trade-off; Phase 1D does not keep server-side state between calls.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from ..errors import IntegrationError
from .config import IntegrationConfig

MAX_PAGES = 10


@dataclass(frozen=True)
class RemoteTool:
    """One tool advertised by an MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]


class MCPConnection(Protocol):
    """The small surface the integration manager depends on."""

    def list_tools(self) -> list[RemoteTool]:
        """Return the tools this server currently advertises."""

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke one tool and return a JSON-serializable result."""

    def close(self) -> None:
        """Release any resources.  Safe to call more than once."""


def _client_session() -> Any:
    try:
        from mcp import ClientSession
    except ImportError:
        raise IntegrationError(
            "The MCP SDK is not installed. Install it with: pip install 'mcp>=1.0'"
        ) from None
    return ClientSession


def _unpack(streams: Any) -> tuple[Any, Any]:
    """Accept both MCP 1.x and 2.x transport return shapes."""
    read = getattr(streams, "read_stream", None)
    write = getattr(streams, "write_stream", None)
    if read is not None and write is not None:
        return read, write
    if isinstance(streams, (tuple, list)) and len(streams) >= 2:
        return streams[0], streams[1]
    raise IntegrationError("The MCP SDK returned an unexpected transport shape.")


def _remote_tool(tool: Any) -> RemoteTool:
    name = str(getattr(tool, "name", "") or "").strip()
    if not name:
        raise IntegrationError("An MCP server advertised a tool without a name.")

    description = str(getattr(tool, "description", "") or "").strip()
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}

    return RemoteTool(name=name, description=description, input_schema=schema)


async def _list_all(session: Any) -> list[Any]:
    """Follow pagination cursors, capped so a broken server cannot loop."""
    try:
        from mcp.types import PaginatedRequestParams
    except ImportError:
        PaginatedRequestParams = None  # type: ignore[assignment]

    tools: list[Any] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        if cursor:
            if PaginatedRequestParams is not None:
                try:
                    result = await session.list_tools(
                        params=PaginatedRequestParams(cursor=cursor)
                    )
                except TypeError:
                    # MCP 1.x took the cursor directly.
                    result = await session.list_tools(cursor=cursor)
            else:
                result = await session.list_tools(cursor=cursor)
        else:
            result = await session.list_tools()

        tools.extend(getattr(result, "tools", None) or [])
        cursor = getattr(result, "next_cursor", None) or getattr(
            result, "nextCursor", None
        )
        if not cursor:
            break
    return tools


def _result_payload(result: Any) -> Any:
    """Convert an MCP CallToolResult into JSON-friendly data."""
    if result is None:
        return {}

    is_error = bool(
        getattr(result, "is_error", False) or getattr(result, "isError", False)
    )
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)

    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(str(block))
    text = "\n".join(part for part in parts if part).strip()

    if is_error:
        detail = text
        if not detail and structured is not None:
            try:
                detail = json.dumps(structured, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                detail = str(structured)
        raise IntegrationError(detail or "The MCP tool returned an error.")

    if structured is not None:
        return structured
    if text:
        return {"text": text}
    return {}


def _run(coro: Any, timeout: float) -> Any:
    """Run a coroutine from sync code, even if this thread already has a loop."""

    async def runner() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(runner())
        except (asyncio.TimeoutError, TimeoutError):
            raise IntegrationError(
                f"The MCP server did not respond within {timeout:g}s."
            ) from None

    # A tool handler can be invoked from an async context.  Use a fresh thread
    # so asyncio.run() still has a loop of its own.
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(lambda: asyncio.run(runner())).result()
        except (asyncio.TimeoutError, TimeoutError):
            raise IntegrationError(
                f"The MCP server did not respond within {timeout:g}s."
            ) from None


class SDKMCPConnection:
    """An MCP client that opens one session per operation."""

    def __init__(self, config: IntegrationConfig) -> None:
        self.config = config

    def list_tools(self) -> list[RemoteTool]:
        return _run(self._list_tools(), self.config.timeout)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return _run(
            self._call_tool(str(name), arguments or {}),
            self.config.timeout,
        )

    def close(self) -> None:
        """Nothing is held between calls."""

    async def _list_tools(self) -> list[RemoteTool]:
        ClientSession = _client_session()
        async with self._streams() as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return [_remote_tool(tool) for tool in await _list_all(session)]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        ClientSession = _client_session()
        async with self._streams() as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                return _result_payload(result)

    @asynccontextmanager
    async def _streams(self) -> AsyncIterator[tuple[Any, Any]]:
        if self.config.transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            if not self.config.command:
                raise IntegrationError(
                    f"Integration '{self.config.name}' has no command."
                )
            params_kwargs: dict[str, Any] = {
                "command": self.config.command[0],
                "args": list(self.config.command[1:]),
                "env": self.config.env or None,
            }
            if self.config.cwd:
                params_kwargs["cwd"] = self.config.cwd
            params = StdioServerParameters(**params_kwargs)
            async with stdio_client(params) as streams:
                yield _unpack(streams)
            return

        module = importlib.import_module("mcp.client.streamable_http")
        client = getattr(module, "streamable_http_client", None)
        if client is None:
            client = getattr(module, "streamablehttp_client", None)
        if client is None:
            raise IntegrationError(
                "This MCP SDK version has no streamable HTTP client."
            )
        async with client(self.config.url) as streams:
            yield _unpack(streams)


def make_connection(config: IntegrationConfig) -> MCPConnection:
    """Build the default connection for one integration."""
    if not config.enabled:
        raise IntegrationError(f"Integration '{config.name}' is disabled.")
    return SDKMCPConnection(config)
