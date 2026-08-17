"""Pre-fill a DCF from what the company actually reported.

A blank DCF is not much use: the hard part is not the arithmetic, it is knowing
what to type into twenty boxes. This reads the filed statements and proposes a
starting point for each one — historical growth, the margin the business
actually runs at, its real capex intensity, its effective tax rate, its net
debt, and a CAPM cost of equity off the live Treasury curve.

Everything it produces is a *default*, and every default reports the history it
came from so the operator can see what they are overriding. Nothing here is a
forecast; it is the past, arranged so a forecast is quick to state.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..core.errors import EmptyDataError
from ..providers import statements as statements_provider
from ..providers import yahoo

# The long-run US equity risk premium. Damodaran's implied ERP has sat in a
# 4–6% band for two decades; 5% is the middle of it and is an assumption the
# operator can override like any other.
EQUITY_RISK_PREMIUM = 0.05
DEFAULT_RISK_FREE = 0.042
FALLBACK_BETA = 1.0

# Growth is faded from what the business has been doing toward the terminal
# rate, because nothing compounds at its trailing rate forever.
DEFAULT_YEARS = 5
DEFAULT_TERMINAL_GROWTH = 0.025
MAX_SEED_GROWTH = 0.35


def _latest(rows: List[Dict[str, Any]], line_item: str, periods: List[str],
            count: int = 1) -> List[float]:
    """Up to ``count`` values for one line, newest first, skipping blanks."""
    row = next((r for r in rows if r["line_item"] == line_item), None)
    if row is None:
        return []
    out: List[float] = []
    for period in periods:
        value = row.get(period)
        if value is not None:
            out.append(float(value))
        if len(out) >= count:
            break
    return out


def _at(rows: List[Dict[str, Any]], line_item: str, period: str) -> Optional[float]:
    """One line at one date.

    Deliberately *not* ``_latest``: a balance sheet has to be read at a single
    moment. Reaching back a year for whichever component happens to be blank
    nets this year's debt against last year's cash, which is how a valuation
    ends up with a number that appears on no filing.
    """
    row = next((r for r in rows if r["line_item"] == line_item), None)
    if row is None:
        return None
    value = row.get(period)
    return float(value) if value is not None else None


def _mean(values: List[float]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _ratio_history(rows: List[Dict[str, Any]], numerator: str, denominator: str,
                   periods: List[str], count: int = 3) -> List[float]:
    """``numerator / denominator`` for each of the last ``count`` periods."""
    num = next((r for r in rows if r["line_item"] == numerator), None)
    den = next((r for r in rows if r["line_item"] == denominator), None)
    if num is None or den is None:
        return []
    out: List[float] = []
    for period in periods:
        a, b = num.get(period), den.get(period)
        if a is None or not b:
            continue
        out.append(float(a) / float(b))
        if len(out) >= count:
            break
    return out


def _cagr(values: List[float]) -> Optional[float]:
    """Compound growth across a newest-first series."""
    series = [v for v in values if v]
    if len(series) < 2:
        return None
    newest, oldest = series[0], series[-1]
    spans = len(series) - 1
    if oldest <= 0 or newest <= 0:
        return None
    return (newest / oldest) ** (1.0 / spans) - 1.0


def _risk_free_rate() -> float:
    """The 10-year Treasury, which is the anchor a cost of equity is built on."""
    try:
        from ..providers import treasury

        rates = treasury.rates()
        for column in ("10_year", "10 yr", "10y", "year_10"):
            if column in rates.columns:
                series = rates[column].dropna()
                if len(series):
                    return float(series.iloc[-1]) / 100.0
        numeric = rates.select_dtypes("number").dropna(axis=1, how="all")
        if not numeric.empty:
            return float(numeric.iloc[-1].dropna().iloc[-1]) / 100.0
    except Exception:  # noqa: BLE001 - a stale-but-sane anchor beats no model
        pass
    return DEFAULT_RISK_FREE


def _fade(start: float, end: float, years: int) -> List[float]:
    """Straight-line path from this year's growth to the terminal rate."""
    if years <= 1:
        return [end]
    return [start + (end - start) * (i / (years - 1)) for i in range(years)]


def seed(symbol: str, years: int = DEFAULT_YEARS,
         period: str = "annual") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(assumptions, evidence)`` for ``symbol``.

    ``evidence`` carries the history each default was read off, so the UI can
    show what is being overridden rather than presenting the numbers as if they
    fell out of the sky.
    """
    symbol = symbol.upper().strip()
    rows, meta = statements_provider.statements(symbol, period=period, limit=6)
    periods: List[str] = meta["periods"]
    if not periods:
        raise EmptyDataError("No filed periods for {}".format(symbol))

    revenue_history = _latest(rows, "revenue", periods, count=6)
    if not revenue_history:
        raise EmptyDataError(
            "{} reports no revenue line, so there is nothing to project.".format(symbol))
    revenue_base = revenue_history[0]

    margin_history = _ratio_history(rows, "operating_income", "revenue", periods)
    tax_history = _ratio_history(rows, "income_tax_expense", "pretax_income", periods)
    dep_history = _ratio_history(rows, "depreciation_and_amortization", "revenue", periods)
    capex_history = _ratio_history(rows, "capital_expenditure", "revenue", periods)

    historical_growth = _cagr(revenue_history)
    if historical_growth is None:
        historical_growth = DEFAULT_TERMINAL_GROWTH
    # A company that just grew 90% is not the seed for a five-year forecast.
    opening_growth = max(min(historical_growth, MAX_SEED_GROWTH), -0.10)

    info: Dict[str, Any] = {}
    try:
        info = yahoo.info(symbol) or {}
    except Exception:  # noqa: BLE001 - market data is a nicety here, not a input
        info = {}

    shares = (_latest(rows, "weighted_average_shares_diluted", periods)
              or _latest(rows, "weighted_average_shares_basic", periods)
              or [info.get("sharesOutstanding")])
    shares_diluted = float(shares[0]) if shares and shares[0] else 0.0

    # Every component of net debt is read at the same balance-sheet date.
    latest = periods[0]
    debt_now = _at(rows, "total_debt", latest)
    cash_now = _at(rows, "cash_and_equivalents", latest)
    short_term_now = _at(rows, "short_term_investments", latest)
    net_debt = (debt_now or 0.0) - (cash_now or 0.0) - (short_term_now or 0.0)

    risk_free = _risk_free_rate()
    beta = float(info.get("beta") or FALLBACK_BETA)
    cost_of_equity = risk_free + beta * EQUITY_RISK_PREMIUM

    market_cap = float(info.get("marketCap") or 0.0)
    debt = debt_now or 0.0
    equity_weight = market_cap / (market_cap + debt) if (market_cap + debt) > 0 else 1.0

    # Interest actually paid against the debt actually carried — both from the
    # same year. Many filers fold interest into "other income, net" and stop
    # tagging it, and dividing a three-year-old interest charge by today's debt
    # is a rate that was never true.
    cost_of_debt = None
    for period in periods:
        paid, carried = _at(rows, "interest_expense", period), _at(rows, "total_debt", period)
        if paid is not None and carried:
            cost_of_debt = abs(paid) / carried
            break
    if cost_of_debt is None:
        cost_of_debt = max(risk_free + 0.01, 0.03)
    cost_of_debt = min(max(cost_of_debt, 0.01), 0.15)

    tax_rate = _mean(tax_history)
    tax_rate = min(max(tax_rate, 0.0), 0.5) if tax_rate is not None else 0.21

    assumptions: Dict[str, Any] = {
        "revenue_base": revenue_base,
        "shares_diluted": shares_diluted,
        "years": years,
        "revenue_growth": [round(g, 4) for g in
                           _fade(opening_growth, DEFAULT_TERMINAL_GROWTH, years)],
        "operating_margin": round(_mean(margin_history[:2]) or 0.15, 4),
        "tax_rate": round(tax_rate, 4),
        "depreciation_pct_revenue": round(_mean(dep_history) or 0.04, 4),
        # capital_expenditure is signed negative by the statements provider.
        "capex_pct_revenue": round(abs(_mean(capex_history) or 0.05), 4),
        "nwc_pct_revenue_change": 0.05,
        "discount_rate": None,
        "equity_weight": round(equity_weight, 4),
        "cost_of_equity": round(cost_of_equity, 4),
        "cost_of_debt": round(cost_of_debt, 4),
        "terminal_method": "perpetuity",
        "terminal_growth": DEFAULT_TERMINAL_GROWTH,
        "exit_multiple": 12.0,
        "net_debt": net_debt,
        "mid_year": True,
    }

    evidence: Dict[str, Any] = {
        "symbol": symbol,
        "name": info.get("longName") or info.get("shortName") or symbol,
        "currency": info.get("currency"),
        "price": info.get("currentPrice") or info.get("regularMarketPrice"),
        "market_cap": market_cap or None,
        "beta": beta,
        "risk_free_rate": round(risk_free, 4),
        "equity_risk_premium": EQUITY_RISK_PREMIUM,
        "periods": periods,
        "provider_by_statement": meta["provider_by_statement"],
        "history": {
            "revenue": revenue_history,
            "revenue_cagr": historical_growth,
            "operating_margin": margin_history,
            "effective_tax_rate": tax_history,
            "depreciation_pct_revenue": dep_history,
            "capex_pct_revenue": [abs(v) for v in capex_history],
            "total_debt": [debt_now] if debt_now is not None else [],
            "cash_and_equivalents": [cash_now] if cash_now is not None else [],
        },
        "notes": [
            "Growth starts at the trailing {} revenue CAGR and fades to the terminal rate."
            .format(len([v for v in revenue_history if v])),
            "Cost of equity is CAPM: {:.2%} risk-free + {:.2f} beta x {:.1%} equity risk premium."
            .format(risk_free, beta, EQUITY_RISK_PREMIUM),
            "Margins, tax, depreciation and capex are averages of the filed history shown.",
        ],
    }
    if not shares_diluted:
        evidence["notes"].append(
            "No diluted share count was filed — enter one before the per-share value means anything.")

    # Net debt is read at one date, so a component the filer stopped tagging
    # counts as zero rather than quietly borrowing an earlier year's figure.
    # That understates net cash, which flatters nothing but is still wrong, so
    # it is said out loud instead of buried in a number.
    absent = [label for label, present in (
        ("total debt", debt_now is not None),
        ("cash", cash_now is not None),
        ("short-term investments", short_term_now is not None),
    ) if not present]
    if absent:
        evidence["notes"].append(
            "The {} balance sheet at {} tags no {} — net debt of {} treats it as nil. "
            "Check the filing and set net debt directly if it matters."
            .format(symbol, latest, " or ".join(absent), _money(net_debt)))
    return assumptions, evidence


def _money(value: float) -> str:
    """A signed figure a reader can scan, in billions where that helps."""
    if abs(value) >= 1e9:
        return "{:,.1f}bn".format(value / 1e9)
    if abs(value) >= 1e6:
        return "{:,.0f}m".format(value / 1e6)
    return "{:,.0f}".format(value)
