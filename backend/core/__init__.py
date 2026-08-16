"""Platform core: registry, providers plumbing, REST/Python interfaces."""
from __future__ import annotations

from .errors import (
    EmptyDataError,
    MFTError,
    MissingCredentialError,
    ProviderError,
    UnknownCommandError,
    UnknownProviderError,
)
from .models import MFTObject, Result
from .registry import REGISTRY, command, coverage, execute, get_spec, resolve_provider, search

__all__ = [
    "REGISTRY",
    "EmptyDataError",
    "MFTError",
    "MFTObject",
    "MissingCredentialError",
    "ProviderError",
    "Result",
    "UnknownCommandError",
    "UnknownProviderError",
    "command",
    "coverage",
    "execute",
    "get_spec",
    "resolve_provider",
    "search",
]
