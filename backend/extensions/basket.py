"""What a sector ETF is actually made of.

A sector line on a performance table — "Technology +3.1%" — is a claim about a
basket, and the basket is where the claim either holds up or falls apart. Three
things routinely make it fall apart, and each has a command here:

* **Concentration.** XLE is two dozen names and its largest two are a third of
  the fund; XLK's top three are more than a third. "The sector moved" can mean
  one company moved, and ``concentration`` puts a number on how close to that
  the fund is — including how many names it takes to reach half the money.
* **What is inside the label.** A GICS sector is not one business. Financials
  is banks *and* exchanges *and* insurers, which do not respond to the same
  rate move in the same direction, and ``industries`` splits the basket the way
  the index classifies it rather than the way the ticker names it.
* **Who did the moving.** ``contribution`` multiplies each holding's return by
  its weight, so the sector's move is decomposed into the names that produced
  it — and the reader can see whether the top two names *are* the move.

``overlap`` answers the fourth question, which is about the reader's portfolio
rather than the fund: owning XLK and QQQ is mostly owning the same shares
twice, and the shared weight says how much.

Every basket is the sponsor's own daily file (:mod:`backend.providers.spdr`),
not a vendor's summary, so the numbers are the fund's rather than an estimate
of it. That also fixes the coverage: SPDR funds, which is what the sectors view
is built on. Yahoo's ten-row summary remains available under ``/etf/holdings``
for everything else.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols, one_symbol
from ..providers import markets, spdr, yahoo

# Pulling a return for every name in a basket is one request per holding. That
# is fine for a sector fund (two dozen to ninety) and not fine for SPY, so the
# decomposition covers the heaviest names and reports the weight it reached.
MAX_PRICED_HOLDINGS = 150
PRICE_WORKERS = 8

# Where a holding's industry classification is read from. The Select Sector
# SPDRs partition the S&P 500 by construction, so its membership table covers
# every equity line in any of them.
DEFAULT_REFERENCE_INDEX = "sp500"

# Comparing the eleven sector funds with each other is what concentration is
# for, so that is what the command does when it is not told otherwise.
SECTOR_FUNDS = ",".join(spdr.SECTOR_FUNDS)


# --------------------------------------------------------------------------- #
# Shared shaping
# --------------------------------------------------------------------------- #
def _basket(symbol: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """One fund's basket plus the metadata a command reports alongside it."""
    df = spdr.fund_holdings(one_symbol(symbol))
    meta = spdr.holdings_meta(df)
    meta["lines"] = int(len(df))
    for kind in ("equity", "cash", "futures", "other"):
        rows = df[df["line_type"] == kind]
        meta["{}_weight".format(kind)] = round(float(rows["weight"].sum()), 6)
        if kind == "equity":
            meta["holdings"] = int(len(rows))
    return df, meta


def _equity_lines(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    rows = df[df["line_type"] == "equity"]
    if rows.empty:
        raise EmptyDataError("{} publishes no equity holdings".format(symbol))
    return rows.reset_index(drop=True)


def _classifications(index: str) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """``{symbol: {sector, industry}}`` from an index membership table."""
    try:
        members = markets.index_constituents(index)
    except Exception as exc:  # noqa: BLE001 - the basket stands without industries
        return {}, "industry classification unavailable ({}: {})".format(index, exc)
    lookup = {
        str(row.symbol).upper(): {
            "sector": getattr(row, "sector", None),
            "industry": getattr(row, "industry", None),
        }
        for row in members.itertuples()
    }
    return lookup, None


def _concentration_stats(weights: Sequence[float]) -> Dict[str, Any]:
    """Concentration of one basket, from its equity weights.

    ``hhi`` is the Herfindahl index on fractions, so ``1 / hhi`` is the number
    of *equally weighted* holdings that would be this concentrated — the
    honest reading of "how many stocks is this really", and usually a fraction
    of the number of names on the list.
    """
    series = pd.Series([w for w in weights if w and w > 0], dtype="float64")
    if series.empty:
        return {}
    ranked = series.sort_values(ascending=False).reset_index(drop=True)
    total = float(ranked.sum())
    hhi = float((ranked**2).sum())
    cumulative = ranked.cumsum()
    stats: Dict[str, Any] = {
        "holdings": int(ranked.size),
        "hhi": round(hhi, 6),
        "effective_holdings": round(1.0 / hhi, 2) if hhi else None,
        "largest_weight": round(float(ranked.iloc[0]), 6),
        "median_weight": round(float(ranked.median()), 6),
        "equal_weight": round(total / ranked.size, 6),
        # How few names it takes to be half the fund. One number, and the one
        # that tends to surprise: XLE reaches half on four holdings.
        "holdings_to_half": int((cumulative < total / 2).sum() + 1),
    }
    for n in (1, 5, 10, 25):
        stats["top_{}_weight".format(n)] = (
            round(float(cumulative.iloc[min(n, ranked.size) - 1]), 6) if ranked.size else None
        )
    return stats


# --------------------------------------------------------------------------- #
# The basket itself
# --------------------------------------------------------------------------- #
@command("/etf/basket/holdings", providers=("ssga",),
         summary="Every line of an ETF's published basket, not just the top ten")
def basket_holdings(symbol: str = "XLK", limit: int = 500, line_type: str = "equity",
                    reference_index: str = DEFAULT_REFERENCE_INDEX,
                    provider: Optional[str] = None) -> Result:
    """The fund's own daily holdings file, heaviest first.

    ``cumulative_weight`` is what the rows above plus this one add up to, which
    is how a reader sees concentration while reading rather than after it:
    where the running total crosses 50% is the point past which the rest of the
    basket is decoration.

    ``line_type`` filters the file's non-stock lines — cash, an index future
    standing in for cash, and the occasional stub left by a takeover. Pass
    ``all`` to see them; they are why the weights sum to just under 100%.
    """
    src = resolve_provider(provider, ("ssga",))
    if line_type not in ("equity", "all"):
        raise ValueError("line_type must be equity or all")
    df, meta = _basket(symbol)
    rows = df if line_type == "all" else _equity_lines(df, one_symbol(symbol))

    classes, warning = _classifications(reference_index)
    out = rows.copy().reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    out["cumulative_weight"] = out["weight"].cumsum().round(6)
    out["sector"] = [classes.get(s, {}).get("sector") for s in out["symbol"]]
    out["industry"] = [classes.get(s, {}).get("industry") for s in out["symbol"]]
    meta["returned"] = int(min(limit, len(out)))
    meta["returned_weight"] = round(float(out["weight"].head(limit).sum()), 6)
    return Result(out.head(limit), provider=src, extra=meta,
                  warnings=[warning] if warning else [])


@command("/etf/basket/concentration", providers=("ssga",),
         summary="How much of an ETF is its largest holdings")
def basket_concentration(symbol: str = SECTOR_FUNDS, provider: Optional[str] = None) -> Result:
    """One row per fund, ranked on how much of each is its ten largest names.

    ``symbol`` accepts a comma-separated list and defaults to all eleven sector
    SPDRs, which is the comparison worth making: it shows which sector bets are
    really bets on two companies.
    """
    src = resolve_provider(provider, ("ssga",))
    symbols = norm_symbols(symbol, limit=25)

    def one(sym: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            df, meta = _basket(sym)
            equity = _equity_lines(df, sym)
        except Exception as exc:  # noqa: BLE001 - one dead fund loses its row
            return None, "{}: {}".format(sym, exc)
        return (
            {
                "symbol": sym,
                "fund_name": meta.get("fund_name"),
                "as_of": meta.get("as_of"),
                **_concentration_stats(equity["weight"].tolist()),
                "cash_weight": meta.get("cash_weight"),
                "largest_holding": equity.iloc[0]["symbol"],
                "largest_holding_name": equity.iloc[0]["name"],
            },
            None,
        )

    # Ranking all eleven sector funds is the point of this command, so the
    # eleven files are fetched together rather than one after another.
    with ThreadPoolExecutor(max_workers=min(PRICE_WORKERS, len(symbols))) as pool:
        gathered = list(pool.map(one, symbols))
    rows = [row for row, _ in gathered if row]
    warnings = [error for _row, error in gathered if error]
    if not rows:
        raise EmptyDataError("No basket available. {}".format("; ".join(warnings)))
    rows.sort(key=lambda r: -(r.get("top_10_weight") or 0))
    return Result(rows, provider=src, warnings=warnings)


@command("/etf/basket/industries", providers=("ssga", "wikipedia"),
         summary="An ETF's basket split by industry rather than by name")
def basket_industries(symbol: str = "XLK", group: str = "industry",
                      reference_index: str = DEFAULT_REFERENCE_INDEX,
                      provider: Optional[str] = None) -> Result:
    """Roll the basket up to its GICS industries (or sectors).

    A sector label hides how many separate businesses it covers: the point of
    this table is that "Financials was up" can be banks and not exchanges, and
    a reader who owns the ETF owns both.
    """
    src = resolve_provider(provider, ("ssga", "wikipedia"))
    if group not in ("industry", "sector"):
        raise ValueError("group must be industry or sector")
    sym = one_symbol(symbol)
    df, meta = _basket(sym)
    equity = _equity_lines(df, sym)

    classes, warning = _classifications(reference_index)
    if not classes:
        raise EmptyDataError(warning or "No industry classification available")
    equity = equity.copy()
    equity[group] = [classes.get(s, {}).get(group) for s in equity["symbol"]]
    unclassified = equity[equity[group].isna()]
    if not unclassified.empty:
        warning = "{} holding(s) are not in the {} membership table".format(
            len(unclassified), reference_index
        )
    equity[group] = equity[group].fillna("Unclassified")

    rows = []
    for label, part in equity.groupby(group, sort=False):
        part = part.sort_values("weight", ascending=False)
        rows.append(
            {
                group: label,
                "weight": round(float(part["weight"].sum()), 6),
                "holdings": int(len(part)),
                "largest": part.iloc[0]["symbol"],
                "largest_weight": round(float(part.iloc[0]["weight"]), 6),
                "members": ", ".join(part["symbol"].head(8)),
            }
        )
    rows.sort(key=lambda r: -r["weight"])
    meta["groups"] = len(rows)
    return Result(rows, provider=src, extra=meta, warnings=[warning] if warning else [])


# --------------------------------------------------------------------------- #
# Who moved it
# --------------------------------------------------------------------------- #
def _holding_returns(symbols: Sequence[str], start: str, end: str) -> Tuple[Dict[str, float], List[str]]:
    """Simple return per symbol over the window, fetched concurrently."""

    def one(sym: str) -> Tuple[str, Optional[float], Optional[str]]:
        try:
            closes = yahoo.history(sym, start, end)["close"].dropna()
            if len(closes) < 2:
                raise EmptyDataError("only {} bar(s) in the window".format(len(closes)))
            return sym, float(closes.iloc[-1]) / float(closes.iloc[0]) - 1.0, None
        except Exception as exc:  # noqa: BLE001 - a dead symbol loses its row, not the table
            return sym, None, "{}: {}".format(sym, exc)

    with ThreadPoolExecutor(max_workers=min(PRICE_WORKERS, max(len(symbols), 1))) as pool:
        gathered = list(pool.map(one, symbols))
    return ({s: r for s, r, _ in gathered if r is not None},
            [e for _s, _r, e in gathered if e])


@command("/etf/basket/contribution", providers=("ssga", "yahoo"),
         summary="Which holdings produced the ETF's move, weight times return")
def basket_contribution(symbol: str = "XLK", start_date: Optional[str] = None,
                        end_date: Optional[str] = None, limit: int = 150,
                        provider: Optional[str] = None) -> Result:
    """Decompose a fund's move into the names that produced it.

    Each holding contributes its weight multiplied by its return, so a 1%
    position that doubled and a 20% position up 5% are put on the same scale —
    and the answer to "was that the sector or was that NVIDIA" stops being a
    matter of opinion.

    Which weight, though, is the whole question. The published one is today's,
    and today's weight is *the result of* the move being decomposed: a name that
    doubled is a bigger share of the fund than it was when the window opened,
    so weighting by it credits the winners twice and overstates the fund's
    return by a third or more. What a contribution needs is the weight the
    position started at, which is recoverable — a holding whose share count did
    not change moved from ``w / (1 + r)`` to ``w`` — so ``start_weight`` is
    backed out that way and is what ``contribution`` uses. Both weights are
    returned; the difference between them is the drift.

    That leaves one assumption rather than two: shares held stayed put through
    the window. Index changes and the quarterly rebalance break it, which is
    why ``extra`` reports ``total_contribution`` next to the fund's own
    ``fund_return`` and the ``unexplained`` gap between them instead of
    presenting the decomposition as exact.
    """
    src = resolve_provider(provider, ("ssga", "yahoo"))
    sym = one_symbol(symbol)
    start, end = date_window(start_date, end_date, default_days=90)
    df, meta = _basket(sym)
    equity = _equity_lines(df, sym).head(min(limit, MAX_PRICED_HOLDINGS))

    returns, warnings = _holding_returns(equity["symbol"].tolist(), str(start), str(end))
    priced = [(row, returns[row.symbol]) for row in equity.itertuples()
              if returns.get(row.symbol) is not None and returns[row.symbol] > -1]
    if not priced:
        raise EmptyDataError(
            "No holding prices for {} over {}..{}. {}".format(sym, start, end, "; ".join(warnings))
        )

    # Undo the drift, then rescale so the reconstructed basket is the same size
    # as the priced part of today's — the cash line is not credited with a move.
    covered = sum(float(row.weight) for row, _ in priced)
    implied = [float(row.weight) / (1.0 + ret) for row, ret in priced]
    scale = covered / sum(implied)
    rows = [
        {
            "symbol": row.symbol, "name": row.name,
            "weight": round(float(row.weight), 6),
            "start_weight": round(start_weight * scale, 6),
            "return": round(ret, 6),
            "contribution": round(start_weight * scale * ret, 6),
        }
        for (row, ret), start_weight in zip(priced, implied)
    ]
    rows.sort(key=lambda r: -r["contribution"])

    total = sum(r["contribution"] for r in rows)
    meta.update(
        {
            "start_date": str(start), "end_date": str(end),
            "priced_holdings": len(rows),
            "covered_weight": round(covered, 6),
            "total_contribution": round(total, 6),
            "top_5_contribution": round(sum(r["contribution"] for r in rows[:5]), 6),
            "gross_positive_contribution": round(
                sum(r["contribution"] for r in rows if r["contribution"] > 0), 6),
            "advancers": sum(1 for r in rows if r["return"] > 0),
            "decliners": sum(1 for r in rows if r["return"] < 0),
        }
    )
    try:
        fund = yahoo.history(sym, str(start), str(end))["close"].dropna()
        meta["fund_return"] = round(float(fund.iloc[-1]) / float(fund.iloc[0]) - 1.0, 6)
        meta["unexplained"] = round(meta["fund_return"] - total, 6)
    except Exception as exc:  # noqa: BLE001 - the decomposition stands without it
        warnings.append("{} price history: {}".format(sym, exc))
    if len(equity) < meta.get("holdings", 0):
        warnings.append(
            "priced the {} heaviest of {} holdings".format(len(equity), meta["holdings"])
        )
    return Result(rows, provider=src, extra=meta, warnings=warnings)


# --------------------------------------------------------------------------- #
# The same shares twice
# --------------------------------------------------------------------------- #
@command("/etf/basket/overlap", providers=("ssga",),
         summary="How much of two ETFs is the same shares at the same weight")
def basket_overlap(symbol: str = "XLK", versus: str = "SPY", limit: int = 25,
                   provider: Optional[str] = None) -> Result:
    """Shared weight between two baskets, name by name.

    Overlap is the sum over every shared holding of the *smaller* of its two
    weights: the part of each fund the other one already gives you. Two funds
    holding the same names in different proportions are not the same fund, and
    that definition is the one that says so.
    """
    src = resolve_provider(provider, ("ssga",))
    left, right = one_symbol(symbol), one_symbol(versus)
    if left == right:
        raise ValueError("Pick two different funds to compare")
    a_df, a_meta = _basket(left)
    b_df, b_meta = _basket(right)
    a = _equity_lines(a_df, left).set_index("symbol")
    b = _equity_lines(b_df, right).set_index("symbol")

    shared = a.index.intersection(b.index)
    rows = [
        {
            "symbol": sym,
            "name": a.loc[sym, "name"],
            "{}_weight".format(left.lower()): round(float(a.loc[sym, "weight"]), 6),
            "{}_weight".format(right.lower()): round(float(b.loc[sym, "weight"]), 6),
            "shared_weight": round(min(float(a.loc[sym, "weight"]), float(b.loc[sym, "weight"])), 6),
        }
        for sym in shared
    ]
    rows.sort(key=lambda r: -r["shared_weight"])
    overlap = sum(r["shared_weight"] for r in rows)
    a_total, b_total = float(a["weight"].sum()), float(b["weight"].sum())
    extra = {
        "symbol": left, "versus": right,
        "fund_name": a_meta.get("fund_name"), "versus_name": b_meta.get("fund_name"),
        "as_of": a_meta.get("as_of"), "versus_as_of": b_meta.get("as_of"),
        "overlap_weight": round(overlap, 6),
        "shared_holdings": len(rows),
        # Overlap as a share of each fund: a small sector fund can sit almost
        # entirely inside a broad one while barely denting it.
        "share_of_{}".format(left.lower()): round(overlap / a_total, 6) if a_total else None,
        "share_of_{}".format(right.lower()): round(overlap / b_total, 6) if b_total else None,
        "holdings": len(a), "versus_holdings": len(b),
        "only_in_{}".format(left.lower()): round(float(a.loc[a.index.difference(shared), "weight"].sum()), 6),
    }
    return Result(rows[:limit], provider=src, extra=extra)
