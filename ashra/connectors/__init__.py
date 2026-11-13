"""External connectors for A.S.H.R.A.

Phase 2E.0 defines the contract every adapter implements.  Real adapters
(Gmail over IMAP, Google Calendar over private iCal) arrive in 2E.1 and 2E.2
and register here without changing the orchestrator, the tools, or the API.
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
from .registry import ConnectorRegistry, IngestResult
from .replay import ReplayConnector

__all__ = [
    "Candidate",
    "Capabilities",
    "Capability",
    "Connector",
    "ConnectorHealth",
    "ConnectorRegistry",
    "IngestResult",
    "NormalisedRecord",
    "PollResult",
    "Record",
    "ReplayConnector",
    "Source",
    "parse_capability",
    "require_capability",
    "validate_connector_id",
]
