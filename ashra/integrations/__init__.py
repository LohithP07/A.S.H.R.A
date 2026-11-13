"""External MCP integrations for A.S.H.R.A.

Phase 1D connects read-only MCP servers and exposes their tools through the
same permissioned registry the local filesystem tools use.
"""

from .config import IntegrationConfig, load_integrations, parse_integration
from .manager import IntegrationManager, IntegrationState
from .mcp_client import MCPConnection, RemoteTool, SDKMCPConnection, make_connection

__all__ = [
    "IntegrationConfig",
    "IntegrationManager",
    "IntegrationState",
    "MCPConnection",
    "RemoteTool",
    "SDKMCPConnection",
    "load_integrations",
    "make_connection",
    "parse_integration",
]
