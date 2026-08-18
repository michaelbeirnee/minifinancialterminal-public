"""Reusable research-workbench building blocks.

The browser is only one consumer. Keeping the context assembler here lets the
CLI, Python interface, thesis evidence freezer and future portfolio workflows
all ask for the same point-in-time research packet.
"""

from .context import build_context

__all__ = ["build_context"]
