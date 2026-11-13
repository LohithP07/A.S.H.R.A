"""Optional scoped filesystem MCP server.

Run it over stdio:

    python -m ashra.mcp_server --root notes

The server exposes only the read-only helpers from :mod:`ashra.tools`.  It is
optional because the core assistant works without the MCP SDK installed; when
the SDK is present, this gives other MCP hosts a safe view of the notes vault.

Both MCP 1.x (``FastMCP``) and 2.x (``MCPServer``) are supported.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .tools import ScopedFilesystem

INSTRUCTIONS = (
    "Read-only access to the A.S.H.R.A notes vault. "
    "Paths are relative to the vault root; files such as config.json, .env, "
    "and anything outside the vault are never exposed."
)


def _server_factory() -> Any:
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer(
            name="ashra-filesystem",
            instructions=INSTRUCTIONS,
        )
    except ImportError:
        pass

    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP(
            name="ashra-filesystem",
            instructions=INSTRUCTIONS,
        )
    except ImportError:
        raise RuntimeError(
            "The MCP SDK is not installed. Install it with: pip install 'mcp>=1.0'"
        ) from None


def build_server(root: Path | None = None) -> Any:
    """Create the MCP server for one filesystem root."""
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent / "notes"
    fs = ScopedFilesystem(base)
    server = _server_factory()

    @server.tool(description="List files and folders inside the notes vault.")
    def fs_list(
        relative_path: str = "",
        recursive: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        """List a directory in the vault without leaving its root."""
        return fs.list_directory(
            relative_path=relative_path,
            recursive=recursive,
            limit=limit,
        )

    @server.tool(description="Read a text or markdown file inside the notes vault.")
    def fs_read(relative_path: str, max_chars: int = 20000) -> dict[str, Any]:
        """Read one allowed text file from the vault."""
        return fs.read_text(relative_path=relative_path, max_chars=max_chars)

    @server.tool(description="Search notes for a phrase and return matching lines.")
    def fs_search(
        query: str,
        limit: int = 20,
        excerpt_chars: int = 200,
    ) -> dict[str, Any]:
        """Search the vault case-insensitively."""
        return fs.search(query=query, limit=limit, excerpt_chars=excerpt_chars)

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ashra-mcp-filesystem",
        description="Serve the A.S.H.R.A notes vault over MCP (stdio).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Vault root to expose. Defaults to the project's notes/ folder.",
    )
    args = parser.parse_args(argv)

    try:
        server = build_server(args.root)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
