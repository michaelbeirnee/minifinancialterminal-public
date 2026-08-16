"""Portfolio-level valuation, return series and risk.

The numbers here are what turns a blotter into a portfolio. Everything is
reconstructed from the transaction log:

* :func:`mark_to_market` values today's holdings against live quotes;
* :func:`value_series` rebuilds the daily equity curve from historical prices
  and the share counts implied by the log;
* :func:`returns_series` turns that curve into a time-weighted return stream,
  which is the input the analytics the platform already has — the risk metrics
  in :mod:`backend.extensions.quantitative` and the factor regressions in
  :mod:`backend.factors.models` — have been waiting for.

Deposits and withdrawals are neutralised day by day (a daily time-weighted
return), so paying money in is never mistaken for performance. The
money-weighted counterpart is :func:`xirr`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from ..core.errors import MFTError
from ..core.registry import execute
from ..data.provider import get_history
from ..models import Transaction
from .accounting import EPSILON, Ledger

TRADING_DAYS = 252


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
def price_panel(
    symbols: Sequence[str], start: str, end: Optional[str] = None
) -> Tuple[pd.DataFrame, List[str]]:
    """Wide close-price frame for ``symbols``, outer-joined and forward-filled.

    Unlike :func:`backend.data.provider.get_price_panel` this never drops a date
    because one holding has no history there — a portfolio routinely owns names
    that listed at different times, and dropping those rows would silently
    shorten the equity curve. Symbols that fail entirely are reported back as
    warnings rather than raising.
    """
    series: Dict[str, pd.Series] = {}
    warnings: List[str] = []
    for symbol in symbols:
        try:
            series[symbol.upper()] = get_history(symbol, start, end)["close"]
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not kill the curve
            warnings.append("{}: {}".format(symbol, exc))
    if not series:
        return pd.DataFrame(), warnings
    panel = pd.DataFrame(series).sort_index()
    panel.index = _naive_index(pd.to_datetime(panel.index))
    return panel.ffill(), warnings


def _naive_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Drop any timezone and the time-of-day, so dates compare cleanly."""
    if index.tz is not None:
        index = index.tz_convert(None)
    return index.normalize()


def live_quotes(symbols: Sequence[str]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Snapshot quotes keyed by symbol, plus any provider warnings."""
    if not symbols:
        return {}, []
    try:
        result = execute("/equity/price/quote", symbol=",".join(sorted(set(symbols))))
    except MFTError as exc:
        return {}, [str(exc)]
    quotes = {row.get("symbol"): row for row in result.to_records() if row.get("symbol")}
    return quotes, list(result.warnings)


# --------------------------------------------------------------------------- #
# Point-in-time valuation
# --------------------------------------------------------------------------- #
def mark_to_market(
    ledger: Ledger, quotes: Dict[str, Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Value open holdings against live quotes.

    Returns ``(rows, totals)``. A holding whose quote is missing is still
    listed, valued at cost, and flagged with ``stale = True`` so the caller can
    say so rather than quietly understating the portfolio.
    """
    rows: List[Dict[str, Any]] = []
    for holding in ledger.open_holdings:
        quote = quotes.get(holding.symbol) or {}
        last = quote.get("last_price")
        prev = quote.get("prev_close")
        stale = last is None
        price = float(last) if last is not None else holding.avg_cost
        market_value = holding.quantity * price
        cost_basis = holding.cost_basis
        unrealized = market_value - cost_basis
        day_change = (
            holding.quantity * (price - float(prev))
            if (prev not in (None, 0) and not stale)
            else None
        )
        rows.append(
            {
                "symbol": holding.symbol,
                "asset_type": holding.asset_type,
                "quantity": holding.quantity,
                "avg_cost": holding.avg_cost,
                "last_price": None if stale else float(last),
                "cost_basis": cost_basis,
                "market_value": market_value,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": (unrealized / abs(cost_basis)) if cost_basis else None,
                "realized_pnl": holding.realized_pnl,
                "dividends": holding.dividends,
                "fees_paid": holding.fees_paid,
                "day_change": day_change,
                "day_change_pct": quote.get("change_percent") if not stale else None,
                "name": quote.get("name"),
                "currency": quote.get("currency"),
                "first_trade_at": holding.first_trade_at,
                "last_trade_at": holding.last_trade_at,
                "stale": stale,
            }
        )

    holdings_value = sum(r["market_value"] for r in rows)
    total_value = holdings_value + ledger.cash
    # Weights are of gross exposure, so a short does not net a long out of the
    # denominator and make the remaining weights read above 100%.
    gross = sum(abs(r["market_value"]) for r in rows) or 1.0
    for row in rows:
        row["weight"] = row["market_value"] / gross
        row["weight_of_total"] = (row["market_value"] / total_value) if total_value else None
    rows.sort(key=lambda r: abs(r["market_value"]), reverse=True)

    day_change = sum(r["day_change"] for r in rows if r["day_change"] is not None)
    prior_value = total_value - day_change
    totals = {
        "holdings_value": holdings_value,
        "cash": ledger.cash,
        "total_value": total_value,
        "cost_basis": sum(r["cost_basis"] for r in rows),
        "unrealized_pnl": sum(r["unrealized_pnl"] for r in rows),
        "realized_pnl": ledger.realized_pnl,
        "dividends": ledger.dividends,
        "fees": ledger.fees,
        "net_deposits": ledger.net_deposits,
        "deposits": ledger.deposits,
        "withdrawals": ledger.withdrawals,
        # What the account is worth beyond the money put into it.
        "total_pnl": total_value - ledger.net_deposits,
        "total_pnl_pct": (
            (total_value - ledger.net_deposits) / ledger.net_deposits
            if ledger.net_deposits
            else None
        ),
        "day_change": day_change,
        "day_change_pct": (day_change / prior_value) if prior_value else None,
        "positions": len(rows),
        "stale_quotes": [r["symbol"] for r in rows if r["stale"]],
    }
    return rows, totals


# --------------------------------------------------------------------------- #
# Historical series
# --------------------------------------------------------------------------- #
def share_frame(
    transactions: Sequence[Transaction], index: pd.DatetimeIndex
) -> pd.DataFrame:
    """Shares held per symbol on each date in ``index``."""
    symbols = sorted({(t.symbol or "").upper() for t in transactions if t.side in ("buy", "sell")})
    frame = pd.DataFrame(0.0, index=index, columns=symbols)
    if not symbols:
        return frame
    for txn in transactions:
        if txn.side not in ("buy", "sell"):
            continue
        signed = float(txn.quantity) * (1.0 if txn.side == "buy" else -1.0)
        stamp = _as_day(txn.trade_date)
        # A trade lands on the first session on or after its date, so a
        # weekend-stamped entry still shows up on the following Monday.
        landed = index[index >= stamp]
        if len(landed) == 0:
            continue
        frame.loc[landed[0], (txn.symbol or "").upper()] += signed
    return frame.cumsum()


def cash_frame(transactions: Sequence[Transaction], index: pd.DatetimeIndex) -> pd.DataFrame:
    """Running cash balance and the external flow that moved it, per date."""
    frame = pd.DataFrame(0.0, index=index, columns=["flow", "external_flow"])
    for txn in transactions:
        stamp = _as_day(txn.trade_date)
        landed = index[index >= stamp]
        if len(landed) == 0:
            continue
        day = landed[0]
        frame.loc[day, "flow"] += txn.cash_flow
        if txn.is_external:
            frame.loc[day, "external_flow"] += txn.cash_flow
    frame["cash"] = frame["flow"].cumsum()
    return frame


def _as_day(value: Any) -> pd.Timestamp:
    """A transaction stamp as a naive midnight timestamp."""
    stamp = pd.Timestamp(value or datetime.utcnow())
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return stamp.normalize()


def value_series(
    transactions: Sequence[Transaction],
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Tuple[pd.DataFrame, List[str]]:
    """Daily equity curve for the portfolio.

    Columns: ``holdings_value``, ``cash``, ``total_value``, ``external_flow``
    and ``return`` (the daily time-weighted return). Empty when the log has no
    transactions.
    """
    if not transactions:
        return pd.DataFrame(), []

    first = min(_as_day(t.trade_date) for t in transactions)
    window_start = pd.Timestamp(start) if start else first
    panel_start = min(first, window_start)
    symbols = sorted({(t.symbol or "").upper() for t in transactions if t.side in ("buy", "sell")})

    panel, warnings = price_panel(symbols, panel_start.date().isoformat(), end)
    if panel.empty:
        # A cash-only account still has a curve — build it on business days.
        stop = pd.Timestamp(end) if end else pd.Timestamp(date.today())
        index = pd.bdate_range(panel_start, max(stop, panel_start))
    else:
        index = panel.index[panel.index >= panel_start]

    if len(index) == 0:
        return pd.DataFrame(), warnings

    shares = share_frame(transactions, index)
    cash = cash_frame(transactions, index)

    if panel.empty or shares.empty:
        holdings_value = pd.Series(0.0, index=index)
    else:
        prices = panel.reindex(index).ffill().bfill()
        aligned = prices.reindex(columns=shares.columns).fillna(0.0)
        holdings_value = (shares * aligned).sum(axis=1)

    frame = pd.DataFrame(
        {
            "holdings_value": holdings_value,
            "cash": cash["cash"],
            "external_flow": cash["external_flow"],
        }
    )
    frame["total_value"] = frame["holdings_value"] + frame["cash"]

    # Time-weighted: strip the day's external flow out of the change, so paying
    # money in shows up as a bigger portfolio, never as a better return.
    prior = frame["total_value"].shift(1)
    gain = frame["total_value"] - prior - frame["external_flow"]
    # A return needs something to be a return *on*. Days where the account
    # opened with no positive equity — trades booked before the deposit that
    # funded them, say — have no defined return, and dividing by that near-zero
    # base would otherwise manufacture one in the thousands of percent.
    priced = prior > EPSILON
    frame["return"] = (gain / prior.where(priced)).fillna(0.0)
    frame = frame.replace([np.inf, -np.inf], 0.0)

    undefined = int((~priced).sum()) - 1  # the first day never has a prior close
    if undefined > 0:
        warnings.append(
            "{} day(s) had no positive opening value and are excluded from the "
            "return series — check that deposits are dated before the trades "
            "they paid for.".format(undefined)
        )

    if start:
        frame = frame[frame.index >= pd.Timestamp(start)]
    return frame, warnings


def returns_series(frame: pd.DataFrame) -> pd.Series:
    """The daily time-weighted return stream, ready for the risk engine."""
    if frame.empty:
        return pd.Series(dtype=float)
    series = frame["return"].copy()
    series.index.name = "date"
    return series


def cumulative_return(frame: pd.DataFrame) -> float:
    """Compounded time-weighted return over the whole window."""
    if frame.empty:
        return 0.0
    return float((1.0 + frame["return"]).prod() - 1.0)


# --------------------------------------------------------------------------- #
# Money-weighted return
# --------------------------------------------------------------------------- #
def xirr(flows: Sequence[Tuple[Any, float]], guess_bounds: Tuple[float, float] = (-0.9999, 10.0)) -> Optional[float]:
    """Annualised internal rate of return for dated cash flows.

    ``flows`` are from the investor's point of view: money paid in is negative,
    money taken out (including the closing value) positive. Returns ``None``
    when the flows never cross zero, which is the honest answer — an IRR does
    not exist for a one-signed series.
    """
    cleaned = [(_as_day(when), float(amount)) for when, amount in flows if amount]
    if len(cleaned) < 2:
        return None
    if not (any(a > 0 for _, a in cleaned) and any(a < 0 for _, a in cleaned)):
        return None

    t0 = min(when for when, _ in cleaned)
    years = [((when - t0).days / 365.0, amount) for when, amount in cleaned]

    def npv(rate: float) -> float:
        return sum(amount / (1.0 + rate) ** t for t, amount in years)

    low, high = guess_bounds
    f_low, f_high = npv(low), npv(high)
    if np.isnan(f_low) or np.isnan(f_high) or f_low * f_high > 0:
        return None
    for _ in range(200):  # bisection — no dependency, always converges here
        mid = (low + high) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            return round(float(mid), 6)
        if f_low * f_mid < 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return round(float((low + high) / 2.0), 6)


def money_weighted_return(
    transactions: Sequence[Transaction], ending_value: float, as_of: Optional[Any] = None
) -> Optional[float]:
    """XIRR of the account: deposits in, withdrawals and closing value out."""
    flows: List[Tuple[Any, float]] = [
        (txn.trade_date, -txn.cash_flow) for txn in transactions if txn.is_external
    ]
    if not flows:
        return None
    flows.append((as_of or datetime.utcnow(), ending_value))
    return xirr(flows)


# --------------------------------------------------------------------------- #
# Exposure
# --------------------------------------------------------------------------- #
def group_exposure(rows: Sequence[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """Aggregate marked-to-market rows into weights by ``key``."""
    gross = sum(abs(r["market_value"]) for r in rows) or 1.0
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        label = row.get(key) or "Unknown"
        bucket = buckets.setdefault(
            label,
            {key: label, "market_value": 0.0, "unrealized_pnl": 0.0, "positions": 0,
             "symbols": []},
        )
        bucket["market_value"] += row["market_value"]
        bucket["unrealized_pnl"] += row["unrealized_pnl"]
        bucket["positions"] += 1
        bucket["symbols"].append(row["symbol"])
    for bucket in buckets.values():
        bucket["weight"] = bucket["market_value"] / gross
    return sorted(buckets.values(), key=lambda b: abs(b["market_value"]), reverse=True)


def classify(symbols: Iterable[str]) -> Dict[str, Dict[str, Optional[str]]]:
    """Sector / industry / country per symbol, best effort.

    Profile lookups are cached by the provider layer, and a symbol the provider
    does not cover simply comes back unclassified rather than failing the call.
    """
    from ..providers import yahoo

    out: Dict[str, Dict[str, Optional[str]]] = {}
    for symbol in symbols:
        try:
            info = yahoo.info(symbol)
        except Exception:  # noqa: BLE001 - classification is a nice-to-have
            info = {}
        out[symbol] = {
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
        }
    return out


def concentration(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """How much of the portfolio rides on how few names."""
    weights = sorted((abs(r["weight"]) for r in rows), reverse=True)
    if not weights:
        return {"herfindahl": None, "effective_positions": None, "top_1": None, "top_5": None}
    hhi = float(sum(w**2 for w in weights))
    return {
        "herfindahl": round(hhi, 6),
        # 1/HHI: the number of equally-weighted names carrying the same risk.
        "effective_positions": round(1.0 / hhi, 2) if hhi else None,
        "top_1": round(weights[0], 6),
        "top_5": round(float(sum(weights[:5])), 6),
    }


def risk_contribution(
    rows: Sequence[Dict[str, Any]], panel: pd.DataFrame
) -> List[Dict[str, Any]]:
    """Each position's share of total portfolio volatility.

    Marginal contribution to risk: ``w_i * (Cov · w)_i / sigma_p``. Weights are
    of gross exposure, matching :func:`mark_to_market`, so the contributions sum
    to the portfolio's own volatility.
    """
    symbols = [r["symbol"] for r in rows if r["symbol"] in panel.columns]
    if len(symbols) < 2:
        return []
    weights = np.array([next(r["weight"] for r in rows if r["symbol"] == s) for s in symbols])
    returns = panel[symbols].pct_change().dropna()
    if len(returns) < 20:
        return []

    cov = returns.cov().to_numpy() * TRADING_DAYS
    portfolio_var = float(weights @ cov @ weights)
    if portfolio_var <= 0:
        return []
    portfolio_vol = float(np.sqrt(portfolio_var))
    marginal = (cov @ weights) / portfolio_vol
    contribution = weights * marginal

    out = []
    for symbol, weight, mcr, ctr in zip(symbols, weights, marginal, contribution):
        out.append(
            {
                "symbol": symbol,
                "weight": round(float(weight), 6),
                "volatility": round(float(returns[symbol].std(ddof=1) * np.sqrt(TRADING_DAYS)), 6),
                "marginal_contribution": round(float(mcr), 6),
                "risk_contribution": round(float(ctr), 6),
                "pct_of_risk": round(float(ctr / portfolio_vol), 6),
            }
        )
    out.sort(key=lambda r: r["risk_contribution"], reverse=True)
    return out
