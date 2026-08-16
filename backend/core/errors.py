"""Error types used across the platform layer.

Providers raise these instead of returning empty frames so that a failure is
always visible to the caller rather than silently becoming "no data".
"""
from __future__ import annotations


class MFTError(Exception):
    """Base class for all platform errors."""

    status_code = 500


class ProviderError(MFTError):
    """A data provider was reachable but could not satisfy the request."""

    status_code = 502


class EmptyDataError(MFTError):
    """The request succeeded but the provider returned no rows."""

    status_code = 404


class UnknownProviderError(MFTError):
    """The caller asked for a provider that is not registered for a command."""

    status_code = 400


class MissingCredentialError(MFTError):
    """An optional provider needs an API key that has not been configured."""

    status_code = 400


class UnknownCommandError(MFTError):
    """No command is registered at the requested path."""

    status_code = 404
