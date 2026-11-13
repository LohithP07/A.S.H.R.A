"""Shared exceptions for the server-side brain."""

from __future__ import annotations


class BrainError(RuntimeError):
    """Base class for expected brain failures."""


class ConfigError(BrainError):
    """config.json is missing, malformed, or has invalid values."""


class NotConfiguredError(BrainError):
    """The brain needs an API key, model, or supported provider."""


class ProviderError(BrainError):
    """The upstream model provider failed."""


class ToolError(BrainError):
    """Base class for tool registry failures."""


class ToolNotFound(ToolError):
    """The requested tool is not registered."""


class PermissionDenied(ToolError):
    """The caller does not hold the permission a tool requires."""


class ApprovalRequired(ToolError):
    """The tool changes state and needs explicit human approval."""


class ToolInputError(ToolError):
    """The tool arguments do not match the declared schema."""


class IntegrationError(BrainError):
    """An external MCP integration could not be reached or returned an error."""


class ConnectorError(BrainError):
    """A connector is missing, misconfigured, or returned unusable data."""


class ConnectorNotFound(ConnectorError):
    """The requested connector is not registered."""


class CapabilityDenied(ConnectorError):
    """A connector was asked to do something it does not declare."""


class RateLimited(ConnectorError):
    """A connector was polled again before its minimum interval elapsed."""


class CredentialError(BrainError):
    """The credential vault could not read or write a reference."""
