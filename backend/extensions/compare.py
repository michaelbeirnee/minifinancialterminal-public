"""Comparison menu: one company read against the companies it competes with.

A single stock page answers "how is this company doing". It cannot answer "is
that good", which is a question about the other companies in the same business —
and that is what this menu is for.

Three commands, meant to be used together and each useful alone:

* ``peers`` decides *who* the group is, blending a vendor classification, the
  filer's own SIC registration and the filings that name this company as
  competition (see :mod:`backend.providers.peers`),
* ``table`` puts the group side by side on size, valuation, growth, margins,
  returns and risk — one row per metric, one column per company, plus the peer
  median to read each of them against,
* ``revenue_mix`` asks what those companies actually sell, which is where a
  peer group most often stops being one.

The group-level comparisons — sectors, styles, asset classes — live next door in
``equity.py``; this file is about a named company and its comparables.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols, one_symbol
from ..providers import peers as peers_provider
from ..providers import segments, yahoo
from .quantitative import risk_metrics, series_frame

# A metric: (key, label, section, source field, format, indent)
#   format decides how a reader is shown the number, not how it is stored —
#   every value stays raw so the same row can be charted or exported.
Metric = Tuple[str, str, str, str, str]

SIZE: Tuple[Metric, ...] = (
    ("market_cap", "Market cap", "Size & valuation", "_market_cap", "money"),
    ("enterprise_value", "Enterprise value", "Size & valuation", "enterpriseValue", "money"),
    ("revenue_ttm", "Revenue (TTM)", "Size & valuation", "totalRevenue", "money"),
    ("trailing_pe", "P/E (trailing)", "Size & valuation", "trailingPE", "multiple"),
    ("forward_pe", "P/E (forward)", "Size & valuation", "forwardPE", "multiple"),
    ("price_to_sales", "Price / sales", "Size & valuation", "priceToSalesTrailing12Months",
     "multiple"),
    ("ev_to_ebitda", "EV / EBITDA", "Size & valuation", "enterpriseToEbitda", "multiple"),
    ("price_to_book", "Price / book", "Size & valuation", "priceToBook", "multiple"),
    ("dividend_yield", "Dividend yield", "Size & valuation", "_dividend_yield", "percent"),
)

QUALITY: Tuple[Metric, ...] = (
    ("revenue_growth", "Revenue growth", "Growth & margins", "revenueGrowth", "percent"),
    ("earnings_growth", "Earnings growth", "Growth & margins", "earningsGrowth", "percent"),
    ("gross_margin", "Gross margin", "Growth & margins", "grossMargins", "percent"),
    ("operating_margin", "Operating margin", "Growth & margins", "operatingMargins", "percent"),
    ("profit_margin", "Net margin", "Growth & margins", "profitMargins", "percent"),
    ("return_on_equity", "Return on equity", "Growth & margins", "returnOnEquity", "percent"),
    ("debt_to_equity", "Debt / equity", "Growth & margins", "debtToEquity", "number"),
    ("free_cash_flow", "Free cash flow", "Growth & margins", "freeCashflow", "money"),
)

# Debt to equity comes back in percentage points (78.4 meaning 0.78x).
_PERCENT_POINTS = ("debtToEquity",)

RISK: Tuple[Metric, ...] = (
    ("total_return", "Total return", "Returns & risk", "total_return", "percent"),
    ("cagr", "Annualised return", "Returns & risk", "cagr", "percent"),
    ("annualised_volatility", "Volatility", "Returns & risk", "annualised_volatility", "percent"),
    ("max_drawdown", "Max drawdown", "Returns & risk", "max_drawdown", "percent"),
    ("sharpe", "Sharpe", "Returns & risk", "sharpe", "number"),
    ("beta_to_subject", "Beta to {subject}", "Returns & risk", "beta_to_subject", "number"),
    ("correlation", "Correlation to {subject}", "Returns & risk", "correlation", "number"),
)

SECTIONS: Tuple[str, ...] = ("Size & valuation", "Growth & margins", "Returns & risk")


# --------------------------------------------------------------------------- #
# Who the group is
# --------------------------------------------------------------------------- #
@command("/equity/compare/peers", providers=("sec", "yahoo"),
         summary="Comparable companies, blended from classification, registration and filings")
def compare_peers(symbol: str, limit: int = 12, years: int = 3,
                  provider: Optional[str] = None) -> Result:
    """Who this company's comparables are, and the evidence for each one.

    Three sources are read and made to agree: the vendor industry
    classification, every SEC registrant filing 10-Ks under the same SIC code,
    and — the only one where somebody has actually said two companies compete —
    filings that name this company next to a competition phrase.

    Rows are ranked on how many sources agree, then on how strongly each one
    voted. ``sources`` says which found the row and ``why`` puts that in a
    sentence; a row found in filings carries the newest one that named it, so
    the claim can be checked. Nothing here is a judgement about whether two
    companies are really comparable — that is the reader's, which is why the
    stock page lets the list be edited.
    """
    src = resolve_provider(provider, ("sec", "yahoo"))
    sym = one_symbol(symbol)
    rows, meta = peers_provider.peer_group(sym, limit=limit, years=years)
    warnings = ["{}: {}".format(name, leg["error"])
                for name, leg in meta["sources"].items() if leg["error"]]
    return Result(rows, provider=src, warnings=warnings, extra=meta)


# --------------------------------------------------------------------------- #
# The group side by side
# --------------------------------------------------------------------------- #
@command("/equity/compare/table", providers=("yahoo",),
         summary="Peers side by side on size, valuation, growth, margins and risk")
def compare_table(symbol: str, start_date: Optional[str] = None,
                  end_date: Optional[str] = None, risk_free_rate: float = 0.0,
                  provider: Optional[str] = None) -> Result:
    """One row per metric, one column per company, newest first.

    ``symbol`` is a list — ``AAPL,MSFT,GOOGL`` — and the first one is the
    subject: the returns section is measured against it, and the ``median``
    column is the median of *the others*, so a company can be read against its
    group without being averaged into it.

    Valuation and growth come from the vendor's snapshot (they are
    trailing-twelve-month figures, so they move between filings); returns, risk
    and correlation are computed here from the price history over the window,
    which defaults to three years.
    """
    src = resolve_provider(provider, ("yahoo",))
    symbols = norm_symbols(symbol, limit=12)
    if len(symbols) < 2:
        raise ValueError("Comparison needs at least two symbols")
    subject = symbols[0]

    snapshots, missing = _snapshots(symbols)
    stats, window = _risk(symbols, start_date, end_date, risk_free_rate)
    if not snapshots and not stats:
        raise EmptyDataError("Nothing to compare: no data for {}".format(", ".join(symbols)))

    rows: List[Dict[str, Any]] = []
    for key, label, section, field, shape in SIZE + QUALITY + RISK:
        source = stats if section == "Returns & risk" else snapshots
        values = {sym: _clean(field, (source.get(sym) or {}).get(field)) for sym in symbols}
        if all(v is None for v in values.values()):
            continue
        row: Dict[str, Any] = {
            "metric": key, "label": label.format(subject=subject), "section": section,
            "format": shape, "indent": 1, "weight": "", "derived": False,
        }
        row.update(values)
        row["median"] = _median(values, exclude=subject)
        rows.append(row)
    if not rows:
        raise EmptyDataError("No comparable metrics for {}".format(", ".join(symbols)))

    return Result(rows, provider=src, warnings=missing, extra={
        "symbols": symbols, "subject": subject, "sections": list(SECTIONS),
        "names": {sym: ((snapshots.get(sym) or {}).get("longName")
                        or (snapshots.get(sym) or {}).get("shortName") or sym)
                  for sym in symbols},
        "window": window, "missing": missing,
    })


def _snapshots(symbols: Sequence[str]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """One vendor snapshot per symbol, gathered concurrently."""
    def one(sym: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
        try:
            return sym, _augment(dict(yahoo.info(sym))), None
        except Exception as exc:  # noqa: BLE001 - a dead symbol loses its column, not the table
            return sym, None, "{}: {}".format(sym, exc)

    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        gathered = list(pool.map(one, symbols))
    found = {sym: info for sym, info, _ in gathered if info}
    return found, [error for _sym, _info, error in gathered if error]


def _risk(symbols: Sequence[str], start_date: Optional[str], end_date: Optional[str],
          risk_free_rate: float) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Return, risk, and each company's relationship with the subject."""
    start, end = date_window(start_date, end_date, default_days=365 * 3)
    try:
        returns = series_frame(",".join(symbols), str(start), str(end), "returns")
    except Exception:  # noqa: BLE001 - the valuation half of the table still stands
        return {}, {"start": str(start), "end": str(end), "observations": 0}

    subject = symbols[0]
    base = returns[subject] if subject in returns.columns else None
    stats: Dict[str, Dict[str, Any]] = {}
    for sym in returns.columns:
        series = returns[sym].dropna()
        if series.size < 20:
            continue
        row = dict(risk_metrics(series, risk_free_rate))
        if base is not None:
            paired = pd.concat([series, base], axis=1, join="inner").dropna()
            if len(paired) > 20 and paired.iloc[:, 1].var():
                row["correlation"] = float(paired.iloc[:, 0].corr(paired.iloc[:, 1]))
                row["beta_to_subject"] = float(
                    paired.iloc[:, 0].cov(paired.iloc[:, 1]) / paired.iloc[:, 1].var())
        stats[str(sym)] = row
    return stats, {"start": str(start), "end": str(end),
                   "observations": int(len(returns))}


def _augment(info: Dict[str, Any]) -> Dict[str, Any]:
    """The two fields the vendor does not give straight.

    A dividend yield arrives as a fraction in some payloads and as percentage
    points in others, and below 1% the two are indistinguishable — 0.35 is
    either Apple's 0.35% or a REIT's 35%. The annual rate and the price are
    unambiguous, so the yield is computed from them wherever both are there.

    A market cap is simply missing from some snapshots, which loses the row a
    company most readers size the group by; shares times price is the same
    number by definition.
    """
    price = (info.get("currentPrice") or info.get("regularMarketPrice")
             or info.get("previousClose"))
    rate = info.get("dividendRate")
    quoted = info.get("dividendYield")
    if price and rate:
        info["_dividend_yield"] = float(rate) / float(price)
    elif quoted is not None:
        # No rate to check against: assume points above 1, a fraction below.
        info["_dividend_yield"] = float(quoted) / 100 if float(quoted) > 1 else float(quoted)

    shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
    info["_market_cap"] = info.get("marketCap") or (
        float(shares) * float(price) if shares and price else None)
    return info


def _clean(field: str, value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number / 100 if field in _PERCENT_POINTS and abs(number) > 1 else number


def _median(values: Dict[str, Optional[float]], exclude: str) -> Optional[float]:
    others = [v for sym, v in values.items() if sym != exclude and v is not None]
    return float(pd.Series(others).median()) if others else None


# --------------------------------------------------------------------------- #
# What the group actually sells
# --------------------------------------------------------------------------- #
@command("/equity/compare/revenue_mix", providers=("sec",),
         summary="What each company in the group sells, as a share of its revenue")
def compare_revenue_mix(symbol: str, dimension: str = "best", limit: int = 8,
                        provider: Optional[str] = None) -> Result:
    """The revenue split of every company in the group, side by side.

    Two companies in the same industry bucket can earn their money in entirely
    different places, and that is usually the thing worth knowing before
    comparing their multiples. Rows are one segment of one company, with its
    share of that company's revenue in its newest filed year.

    ``dimension`` is ``business``, ``geographic``, ``product``, or ``best`` —
    which takes each company's reportable segments where it files them and falls
    back to its product or geographic split where it does not, so a group is
    rarely empty. Read from the filings themselves; see
    :mod:`backend.providers.segments`.
    """
    src = resolve_provider(provider, ("sec",))
    symbols = norm_symbols(symbol, limit=12)
    if dimension not in ("best",) + segments.ORDER:
        raise ValueError("dimension must be best, {} or {}".format(
            ", ".join(segments.ORDER[:-1]), segments.ORDER[-1]))

    def one(sym: str) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
        try:
            rows, meta = segments.revenue_segments(
                sym, period="annual", limit=1,
                dimension="all" if dimension == "best" else dimension)
        except Exception as exc:  # noqa: BLE001 - one company short is not a failure
            return sym, [], str(exc)
        return sym, _mix_rows(sym, rows, meta, dimension, limit), None

    with ThreadPoolExecutor(max_workers=min(6, len(symbols))) as pool:
        gathered = list(pool.map(one, symbols))

    rows = [row for _sym, found, _error in gathered for row in found]
    missing = {sym: error for sym, _found, error in gathered if error}
    if not rows:
        raise EmptyDataError(
            "None of {} files a revenue breakdown. Single-segment companies have "
            "nothing to split.".format(", ".join(symbols)))
    return Result(rows, provider=src, extra={
        "symbols": symbols, "dimension": dimension, "missing": missing,
        "covered": sorted({row["symbol"] for row in rows}),
    })


def _mix_rows(symbol: str, rows: List[Dict[str, Any]], meta: Dict[str, Any],
              dimension: str, limit: int) -> List[Dict[str, Any]]:
    """One company's newest split, largest segment first.

    Where the caller asked for the best available breakdown, reportable segments
    win: they are the split the company manages itself by, and the one its
    competitors are most likely to file too.
    """
    reported = [d["dimension"] for d in meta["dimensions"]]
    if dimension != "best":
        chosen = dimension
    else:
        chosen = next((d for d in segments.ORDER if d in reported), None)
    if chosen is None:
        return []

    period = meta["periods"][0]
    total = next((r[period] for r in rows if r["dimension"] == "total"), None)
    members = [r for r in rows
               if r["dimension"] == chosen and r["weight"] == "" and r[period] is not None]
    out = [
        {
            "symbol": symbol, "dimension": chosen,
            "section": segments.SECTIONS[chosen], "segment": row["segment"],
            "revenue": row[period],
            "share": round(row[period] / total, 4) if total else None,
            "period_ending": period,
        }
        for row in members[:limit]
    ]
    covered = sum(row["share"] or 0 for row in out)
    if out and covered < 0.995:
        # The tail the caller did not ask for, so the shares still make a whole.
        out.append({
            "symbol": symbol, "dimension": chosen, "section": segments.SECTIONS[chosen],
            "segment": "Other / undisclosed", "revenue": None,
            "share": round(1 - covered, 4), "period_ending": period,
        })
    return out
