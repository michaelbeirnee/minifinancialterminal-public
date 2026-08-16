"""One consistent as-of snapshot of a portfolio.

``/risk``, ``/factors`` and ``/hedge/*`` all need the same raw material — the
equity curve, the marked-to-market book, live quotes and a price panel. Each
endpoint fetching its own copy means two calls made seconds apart can disagree
about what the book is worth, and a hedge sized against one snapshot would be
ranked against another. This module fetches everything once, in one order, and
hands the callers a single frozen view (see docs/hedge-construction.md, build
order step 1).

The snapshot only *acquires* data. Estimation — risk metrics, factor
regressions, exposure targets — stays with the callers, so the numbers each
endpoint reports keep coming from the same code they always did.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from ..data.provider import get_history
from ..models import Portfolio, Transaction
from . import analytics
from .accounting import Ledger, build_ledger


@dataclass
class Snapshot:
    """Everything the analytics endpoints read, frozen at one instant."""

    portfolio_id: int
    name: str
    base_currency: str
    benchmark: Optional[str]
    start: Optional[str]
    end: Optional[str]
    #: Wall-clock instant the snapshot was assembled (quotes are as of this).
    taken_at: datetime
    #: Daily equity curve (``value_series`` output).
    frame: pd.DataFrame
    #: Daily time-weighted returns derived from ``frame``.
    returns: pd.Series
    ledger: Ledger
    quotes: Dict[str, Dict[str, Any]]
    #: Marked-to-market holdings, sorted by |market value|.
    rows: List[Dict[str, Any]]
    totals: Dict[str, Any]
    #: Close-price panel for the open holdings over the curve's window.
    panel: pd.DataFrame
    #: Benchmark closes aligned to the curve's dates (None when no benchmark
    #: was requested or its history could not be fetched). The shock engine
    #: needs levels, not just returns.
    benchmark_closes: Optional[pd.Series]
    #: Benchmark daily returns, derived from ``benchmark_closes``.
    benchmark_returns: Optional[pd.Series]
    warnings: List[str]

    @property
    def as_of(self) -> Optional[str]:
        """Last priced date of the equity curve."""
        return self.frame.index[-1].date().isoformat() if len(self.frame) else None

    @property
    def total_value(self) -> float:
        return float(self.totals["total_value"])

    @property
    def symbols(self) -> List[str]:
        return [r["symbol"] for r in self.rows]


def build(
    portfolio: Portfolio,
    transactions: Sequence[Transaction],
    start: Optional[str] = None,
    end: Optional[str] = None,
    benchmark: Optional[str] = None,
) -> Snapshot:
    """Assemble the snapshot: curve, ledger, quotes, valuation, price panel.

    Warning order (value series, then quotes, then panel) matches what the
    endpoints historically reported, so refactoring them onto the snapshot
    changes nothing in their responses.
    """
    taken_at = datetime.now(timezone.utc)
    frame, warnings = analytics.value_series(transactions, start, end)
    returns = analytics.returns_series(frame)

    ledger = build_ledger(transactions, portfolio.cost_basis_method)
    quotes, quote_warnings = analytics.live_quotes(
        [h.symbol for h in ledger.open_holdings]
    )
    warnings.extend(quote_warnings)
    rows, totals = analytics.mark_to_market(ledger, quotes)

    panel = pd.DataFrame()
    symbols = [r["symbol"] for r in rows]
    if symbols and len(frame):
        panel, panel_warnings = analytics.price_panel(
            symbols, frame.index[0].date().isoformat(), end
        )
        warnings.extend(panel_warnings)

    bench_symbol = benchmark.upper() if benchmark else None
    benchmark_closes = None
    benchmark_returns = None
    if bench_symbol and len(frame):
        benchmark_closes = _benchmark_closes(bench_symbol, frame.index, warnings)
        if benchmark_closes is not None:
            benchmark_returns = benchmark_closes.pct_change().fillna(0.0)

    return Snapshot(
        portfolio_id=portfolio.id,
        name=portfolio.name,
        base_currency=portfolio.base_currency,
        benchmark=bench_symbol,
        start=start,
        end=end,
        taken_at=taken_at,
        frame=frame,
        returns=returns,
        ledger=ledger,
        quotes=quotes,
        rows=rows,
        totals=totals,
        panel=panel,
        benchmark_closes=benchmark_closes,
        benchmark_returns=benchmark_returns,
        warnings=warnings,
    )


def _benchmark_closes(
    symbol: str, index: pd.DatetimeIndex, warnings: List[str]
) -> Optional[pd.Series]:
    """Benchmark closes on exactly the portfolio's own dates.

    Same alignment convention as the ``/performance`` benchmark block:
    reindexed to the equity curve and filled, so a hedge beta is regressed on
    the dates the book was actually marked.
    """
    try:
        closes = get_history(
            symbol,
            index[0].date().isoformat(),
            (index[-1].date() + timedelta(days=1)).isoformat(),
        )["close"]
    except Exception as exc:  # noqa: BLE001 - a missing benchmark is not fatal
        warnings.append("Benchmark {} unavailable: {}".format(symbol, exc))
        return None
    closes.index = analytics._naive_index(pd.to_datetime(closes.index))
    return closes.reindex(index).ffill().bfill()
