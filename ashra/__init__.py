"""Server-side assistant for the knowledge galaxy.

The package is intentionally kept separate from the viewer and from any one
model provider.  That keeps the notes, retrieval, conversation memory, tool
registry, orchestrator, and model transport independently replaceable.
"""

from .agents import (
    SPAWN_TOOL_NAME,
    AgentRegistry,
    AgentResult,
    AgentSpec,
    SubagentRunner,
    build_default_agents,
    build_spawn_tool,
)
from .chat import ChatResult, ChatService
from .config import AppConfig, load_config
from .db import Database, SQLiteSessionStore
from .errors import BrainError, ConfigError, NotConfiguredError, ProviderError
from .integrations import IntegrationConfig, IntegrationManager, load_integrations
from .orchestrator import Orchestrator, OrchestratorResult
from .tools import Permission, ToolRegistry, build_default_registry

__version__ = "0.5.0"

__all__ = [
    "AgentRegistry",
    "AgentResult",
    "AgentSpec",
    "AppConfig",
    "BrainError",
    "ChatResult",
    "ChatService",
    "ConfigError",
    "Database",
    "IntegrationConfig",
    "IntegrationManager",
    "NotConfiguredError",
    "Orchestrator",
    "OrchestratorResult",
    "Permission",
    "ProviderError",
    "SPAWN_TOOL_NAME",
    "SQLiteSessionStore",
    "SubagentRunner",
    "ToolRegistry",
    "build_default_agents",
    "build_default_registry",
    "build_spawn_tool",
    "load_config",
    "load_integrations",
]
