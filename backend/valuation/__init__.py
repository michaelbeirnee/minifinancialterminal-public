"""Valuation models the operator drives.

``dcf`` is the arithmetic — pure, fast, and the only place a number is
computed. ``seed`` reads the filed statements and proposes a starting point for
every assumption, so a model opens pre-filled rather than blank.
"""
from __future__ import annotations

from . import dcf, seed

__all__ = ["dcf", "seed"]
