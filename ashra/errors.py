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
