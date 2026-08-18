"""Command extensions.

Importing this package registers every command with
:mod:`backend.core.registry`, which is what builds the REST routes, the Python
interface and the CLI menus. Add a module here and list it in ``MODULES``.
"""
from __future__ import annotations

import importlib
from typing import List

MODULES: List[str] = [
    "overview",
    "research",
    "equity",
    "equity_fundamental",
    "compare",
    "relationships",
    "etf",
    "basket",
    "crypto",
    "currency",
    "derivatives",
    "index",
    "news",
    "sentiment",
    "calendar",
    "economy",
    "fed",
    "fed_signals",
    "fixedincome",
    "commodity",
    "regulators",
    "congress",
    "technical",
    "screener",
    "quantitative",
    "econometrics",
    "charting",
    "thesis_candidates",
    "thesis_propagation",
    "thesis_pairs",
    "thesis_signals",
    "flagged",
]

_loaded = False


def load_all() -> int:
    """Import every extension module once; returns the command count."""
    global _loaded
    from ..core.registry import REGISTRY

    if not _loaded:
        for name in MODULES:
            importlib.import_module("{}.{}".format(__name__, name))
        _loaded = True
    return len(REGISTRY)


load_all()
