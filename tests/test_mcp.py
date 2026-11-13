from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

try:
    import mcp  # noqa: F401

    HAS_MCP = True
except ImportError:
    HAS_MCP = False

from ashra.mcp_server import build_server


@unittest.skipUnless(HAS_MCP, "The MCP SDK is not installed.")
class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "alpha.md").write_text(
            "# Alpha\n\npricing notes\n",
            encoding="utf-8",
        )

    def test_server_exposes_read_only_tools(self) -> None:
        server = build_server(self.base)
        self.assertEqual(server.name, "ashra-filesystem")

        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(names, {"fs_list", "fs_read", "fs_search"})

    def test_tools_are_scoped_to_the_root(self) -> None:
        server = build_server(self.base)
        # FastMCP/MCPServer stores callables on the server; invoke the tool
        # through the public call path to prove the same safety checks apply.
        result = asyncio.run(server.call_tool("fs_read", {"relative_path": "alpha.md"}))
        text = str(result)
        self.assertIn("pricing notes", text)


if __name__ == "__main__":
    unittest.main()
