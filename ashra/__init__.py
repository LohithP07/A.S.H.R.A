"""Server-side brain for the knowledge galaxy.

The package is intentionally kept separate from the viewer and from any one
model provider.  That keeps the notes, retrieval, conversation memory, and
model transport independently replaceable when this becomes a personal
assistant.
"""

from .chat import ChatResult, ChatService
from .config import AppConfig, load_config
from .errors import BrainError, ConfigError, NotConfiguredError, ProviderError

__all__ = [
    "AppConfig",
    "BrainError",
    "ChatResult",
    "ChatService",
    "ConfigError",
    "NotConfiguredError",
    "ProviderError",
    "load_config",
]
