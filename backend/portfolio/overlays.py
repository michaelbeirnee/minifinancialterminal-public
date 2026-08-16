"""CBOE strategy-index overlays: what systematically hedging has actually cost.

Step 7 of docs/hedge-construction.md. We have no historical option chains, so
an options overlay cannot be honestly backtested from our own data. What we do
have are CBOE's published strategy indexes, which have run these rules
continuously for decades:

* ``^PPUT`` — 5% OTM protective put, rolled monthly. **Protective.**
* ``^CLL``  — 95-110 collar (buy 5% OTM put, sell 10% OTM call). **Protective.**
* ``^BXM``  — buy-write (covered call). **NOT protective.**
* ``^PUT``  — put-write (sell cash-secured puts). **NOT protective** — it
  takes downside risk on rather than shedding it.

Overwrite and put-write strategies supply no loss floor, so they come back in
a separate ``comparators`` list and can never be ranked beside a protective
overlay. The segregation lives in the data (:data:`Overlay` carries
``protective``), not in a caller's good intentions.

**Measured data constraint (2026-08-13):** Yahoo serves ``^BXM``, ``^PUT``
and ``^SP500TR``, but returns no usable history for ``^PPUT`` or ``^CLL``.
So in practice this endpoint usually cannot evaluate *protective* hedging at
all — only the premium-selling contrast. The response says so outright via
``protective_available``; reporting an empty ``overlays`` list without that
flag would read as "we checked and protection looks bad", which is the
opposite of the truth. Absence is reported, never fabricated.

The reference defaults to ``^SP500TR``, the S&P 500 **total return** index,
because the strategy indexes include dividends: measuring them against a
price-return benchmark like ``^GSPC`` would flatter every overlay by roughly
the dividend yield each year.

The comparable number is the same shape as the live cost table's:
``cagr_give_up_per_drawdown_removed`` — annual return surrendered per unit of
peak-to-trough loss avoided.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..extensions.quantitative import risk_metrics

#: Default reference: total return, matching what the overlays measure.
DEFAULT_REFERENCE = "^SP500TR"

#: Fewest overlapping sessions worth comparing at all.
MIN_SESSIONS = 250


@dataclass(frozen=True)
class Overlay:
    symbol: str
    name: str
    rule: str
    #: False for overwrite strategies, which provide no downside floor.
    protective: bool


OVERLAYS = (
    Overlay("^PPUT", "5% OTM protective put",
            "Long S&P 500, buy a 5% out-of-the-money put each month.", True),
    Overlay("^CLL", "95-110 collar",
            "Long S&P 500, buy a 5% OTM put funded by selling a 10% OTM call.", True),
    Overlay("^BXM", "Buy-write (covered call)",
            "Long S&P 500, sell an at-the-money call each month.", False),
    Overlay("^PUT", "Put-write",
            "Sell cash-secured at-the-money puts on the S&P 500 each month.", False),
)


def compare(
    panel: pd.DataFrame,
    reference: str,
    overlays: Sequence[Overlay] = OVERLAYS,
    risk_free_rate: float = 0.0,
) -> Dict[str, Any]:
    """Rank protective overlays by cost per unit of drawdown removed.

    ``panel`` is a wide close-price frame containing ``reference`` and each
    overlay symbol. Rows are computed only over dates where the overlay and
    the reference both traded, so no overlay is credited with a period it did
    not live through.
    """
    if reference not in panel.columns:
        raise ValueError("Reference {} is missing from the price panel".format(reference))

    protective: List[Dict[str, Any]] = []
    comparators: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for overlay in overlays:
        if overlay.symbol not in panel.columns:
            skipped.append({"symbol": overlay.symbol, "reason": "no price history available"})
            continue
        pair = panel[[overlay.symbol, reference]].dropna()
        if len(pair) < MIN_SESSIONS:
            skipped.append(
                {
                    "symbol": overlay.symbol,
                    "reason": "only {} overlapping sessions with {}".format(len(pair), reference),
                }
            )
            continue
        row = _row(overlay, pair[overlay.symbol], pair[reference], risk_free_rate)
        (protective if overlay.protective else comparators).append(row)

    protective.sort(key=lambda r: (
        r["cagr_give_up_per_drawdown_removed"]
        if r["cagr_give_up_per_drawdown_removed"] is not None
        else float("inf")
    ))

    notes = [
        "These are CBOE's published strategy indexes, not our own backtest — we "
        "have no historical option chains, so this is the honest way to ask what "
        "running a hedge continuously has cost.",
    ]
    if not protective:
        missing = ", ".join(o.symbol for o in overlays if o.protective)
        notes.insert(
            0,
            "NO PROTECTIVE OVERLAY COULD BE EVALUATED: our data source publishes no "
            "usable history for {}. The rows below sell premium and set no floor, so "
            "nothing here is evidence about what protection costs — read this as the "
            "contrast, not the answer.".format(missing),
        )

    return {
        "reference": reference,
        # Stated explicitly: an empty overlay list means "could not measure",
        # never "measured and found wanting".
        "protective_available": bool(protective),
        "overlays": protective,
        "comparators": comparators,
        "skipped": skipped,
        "notes": notes + [
            "The reference is a total-return index because the overlays include "
            "dividends; comparing them to a price-return benchmark would flatter "
            "every one of them.",
            "Index rules are fixed (monthly roll, fixed moneyness) and frictionless "
            "at the index level — a real overlay pays spreads and cannot always "
            "trade the same strikes.",
            "Buy-write and put-write are listed separately as comparators: selling "
            "premium earns a return but sets no floor, so neither is a hedge and "
            "neither is ever ranked against one.",
        ],
    }


def _row(
    overlay: Overlay, prices: pd.Series, reference: pd.Series, risk_free_rate: float
) -> Dict[str, Any]:
    overlay_returns = prices.pct_change().dropna()
    reference_returns = reference.pct_change().dropna()
    common = overlay_returns.index.intersection(reference_returns.index)
    overlay_returns, reference_returns = overlay_returns[common], reference_returns[common]

    overlay_metrics = risk_metrics(overlay_returns, risk_free_rate)
    reference_metrics = risk_metrics(reference_returns, risk_free_rate)

    give_up = reference_metrics["cagr"] - overlay_metrics["cagr"]
    # Drawdowns are negative; "removed" is how much shallower the worst fall was.
    drawdown_removed = abs(reference_metrics["max_drawdown"]) - abs(
        overlay_metrics["max_drawdown"]
    )
    down = reference_returns < 0
    up = reference_returns > 0

    return {
        "symbol": overlay.symbol,
        "name": overlay.name,
        "rule": overlay.rule,
        "protective": overlay.protective,
        "period": {
            "start": str(common[0].date()),
            "end": str(common[-1].date()),
            "sessions": int(len(common)),
            "years": round(len(common) / 252.0, 1),
        },
        "cagr": round(overlay_metrics["cagr"], 6),
        "reference_cagr": round(reference_metrics["cagr"], 6),
        "cagr_give_up": round(give_up, 6),
        "max_drawdown": round(overlay_metrics["max_drawdown"], 6),
        "reference_max_drawdown": round(reference_metrics["max_drawdown"], 6),
        "drawdown_removed": round(drawdown_removed, 6),
        # The comparable number, mirroring the live table's cost-per-protection.
        "cagr_give_up_per_drawdown_removed": (
            round(give_up / drawdown_removed, 4) if drawdown_removed > 0 else None
        ),
        "volatility": round(overlay_metrics["annualised_volatility"], 6),
        "reference_volatility": round(reference_metrics["annualised_volatility"], 6),
        "sharpe": overlay_metrics.get("sharpe"),
        "reference_sharpe": reference_metrics.get("sharpe"),
        "downside_capture": _capture(overlay_returns[down], reference_returns[down]),
        "upside_capture": _capture(overlay_returns[up], reference_returns[up]),
    }


def _capture(overlay_returns: pd.Series, reference_returns: pd.Series) -> Optional[float]:
    """Share of the reference's move the overlay took, in one direction."""
    denominator = float(reference_returns.sum())
    if not denominator:
        return None
    return round(float(overlay_returns.sum()) / denominator, 4)
