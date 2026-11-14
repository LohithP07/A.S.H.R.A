"""Server-side assistant for the knowledge galaxy.

The package is intentionally kept separate from the viewer and from any one
model provider.  That keeps the notes, retrieval, conversation memory, tool
registry, orchestrator, connectors, and model transport independently
replaceable.
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
from .connectors import (
    Capabilities,
    Capability,
    Connector,
    ConnectorHealth,
    ConnectorRegistry,
    IngestResult,
    PollResult,
    Record,
    ReplayConnector,
    Source,
)
from .credentials import CredentialVault, MemoryVault, get_vault
from .db import Database, SQLiteSessionStore
from .errors import BrainError, ConfigError, NotConfiguredError, ProviderError
from .integrations import IntegrationConfig, IntegrationManager, load_integrations
from .ollama import OllamaProvider, local_health
from .orchestrator import Orchestrator, OrchestratorResult
from .router import LANE_CLOUD, LANE_LOCAL, LANE_REFUSAL, LaneRouter, Route
from .tools import Permission, ToolRegistry, build_default_registry

__version__ = "0.8.0"

__all__ = [
    "AgentRegistry",
    "AgentResult",
    "AgentSpec",
    "AppConfig",
    "BrainError",
    "Capabilities",
    "Capability",
    "ChatResult",
    "ChatService",
    "ConfigError",
    "Connector",
    "ConnectorHealth",
    "ConnectorRegistry",
    "CredentialVault",
    "Database",
    "IngestResult",
    "IntegrationConfig",
    "IntegrationManager",
    "LANE_CLOUD",
    "LANE_LOCAL",
    "LANE_REFUSAL",
    "LaneRouter",
    "MemoryVault",
    "NotConfiguredError",
    "OllamaProvider",
    "Orchestrator",
    "OrchestratorResult",
    "Permission",
    "PollResult",
    "ProviderError",
    "Record",
    "ReplayConnector",
    "Route",
    "SPAWN_TOOL_NAME",
    "SQLiteSessionStore",
    "Source",
    "SubagentRunner",
    "ToolRegistry",
    "build_default_agents",
    "build_default_registry",
    "build_spawn_tool",
    "get_vault",
    "load_config",
    "load_integrations",
    "local_health",
]
