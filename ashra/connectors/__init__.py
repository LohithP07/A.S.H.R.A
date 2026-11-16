"""External connectors for A.S.H.R.A.

Phase 2E.0 defines the contract every adapter implements.  Phase 2E.1 adds the
first real adapter: read-only Gmail over IMAP.  Adapters register here without
changing the orchestrator, the tools, or the API.
"""

from .base import (
    Candidate,
    Capabilities,
    Capability,
    Connector,
    ConnectorHealth,
    NormalisedRecord,
    PollResult,
    Record,
    Source,
    parse_capability,
    require_capability,
    validate_connector_id,
)
from .config import MailboxConfig, load_mailbox_config, parse_mailbox_config
from .gmail import GmailConnector, MemoryCursorStore
from .registry import ConnectorRegistry, IngestResult
from .replay import ReplayConnector

__all__ = [
    "Candidate",
    "Capabilities",
    "Capability",
    "Connector",
    "ConnectorHealth",
    "ConnectorRegistry",
    "GmailConnector",
    "IngestResult",
    "MailboxConfig",
    "MemoryCursorStore",
    "NormalisedRecord",
    "PollResult",
    "Record",
    "ReplayConnector",
    "Source",
    "load_mailbox_config",
    "parse_capability",
    "parse_mailbox_config",
    "require_capability",
    "validate_connector_id",
]
