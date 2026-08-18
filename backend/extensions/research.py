"""Research Workbench commands."""
from __future__ import annotations

from typing import Optional

from ..core.models import Result
from ..core.registry import command
from ..research import context as research_context


@command("/research/context", providers=("mft",),
         summary="Joined top-down and bottom-up research context for one security")
def context(symbol: str, benchmark: str = "SPY", horizon: str = "three_month",
            provider: Optional[str] = None) -> Result:
    """Build a traceable context packet for the expandable Research Workbench.

    ``horizon`` is one_month, three_month, ytd or one_year. The output names
    every source call, keeps top-down and bottom-up evidence separate, and
    marks the exposure bridge unresolved until a causal link is demonstrated.
    """
    if provider not in (None, "mft"):
        raise ValueError("provider must be mft")
    payload = research_context.build_context(symbol, benchmark, horizon)
    return Result(payload, provider="mft", warnings=payload["warnings"],
                  extra={"schema": payload["schema"]})
