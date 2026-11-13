"""Server-side assistant for the knowledge galaxy.

The package is intentionally kept separate from the viewer and from any one
model provider.  That keeps the notes, retrieval, conversation memory, tool
registry, orchestrator, and model transport independently replaceable.
"""

from .chat import ChatResult, ChatService
from .config import AppConfig, load_config
from .db import Database, SQLiteSessionStore
from .errors import BrainError, ConfigError, NotConfiguredError, ProviderError
from .orchestrator import Orchestrator, OrchestratorResult
from .tools import Permission, ToolRegistry, build_default_registry

__version__ = "0.3.0"

__all__ = [
    "AppConfig",
    "BrainError",
    "ChatResult",
    "ChatService",
    "ConfigError",
    "Database",
    "NotConfiguredError",
    "Orchestrator",
    "OrchestratorResult",
    "Permission",
    "ProviderError",
    "SQLiteSessionStore",
    "ToolRegistry",
    "build_default_registry",
    "load_config",
]
