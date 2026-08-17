"""Additional market-wide funnels for the thesis generator.

Insider and congressional activity answer *who is acting*.  These screens add
three other useful ways into an idea: what looks statistically cheap, where
growth and valuation disagree, where the short side is crowded, and where
price has moved far enough to demand an explanation.  They remain attention
signals.  The shared triage and deep-dive layers decide whether any row can be
turned into a falsifiable company claim.

Every command normalises its provider-specific fields into a small stable row
shape and records the emitted rows in thesis memory.  That lets each category
earn (or fail to earn) a measured base rate instead of relying permanently on
the screen's intuition.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..providers import yahoo
from ..thesis import sources


_ATTENTION_ONLY = (
    "Screen membership is an attention signal, not a recommendation. Provider "
    "fundamentals, estimates and short-interest fields can be stale or use "
    "different reporting periods; verify the mechanism in filings and current "
    "market data before promoting a thesis."
)


def _first(row: Mapping[str, Any], names: Iterable[str]) -> Any:
    """First non-empty field, accepting Yahoo's several naming generations."""
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> Optional[float]:
    """Provider numbers may be scalars or ``{"raw": ...}`` display objects."""
    if isinstance(value, Mapping):
        value = value.get("raw")
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _fraction(value: Any) -> Optional[float]:
    """Normalise fields that Yahoo has returned as both 0.25 and 25.0."""
    number = _number(value)
    if number is None:
        return None
    return number / 100.0 if abs(number) > 1.0 else number


_ALIASES: Dict[str, Sequence[str]] = {
    "symbol": ("symbol", "ticker"),
    "issuer": ("longName", "shortName", "displayName", "name", "companyName"),
    "last_price": ("regularMarketPrice", "intradayprice", "last_price", "lastsale"),
    "market_cap": ("marketCap", "intradaymarketcap", "market_cap", "marketcap"),
    "pe_ratio": ("trailingPE", "peratio.lasttwelvemonths", "pe_ratio"),
    "forward_pe": ("forwardPE", "forward_pe"),
    "peg_ratio": ("pegRatio", "pegratio_5y", "peg_ratio"),
    "eps_growth": (
        "earningsGrowth", "epsGrowth", "epsgrowth.lasttwelvemonths", "eps_growth"
    ),
    "revenue_growth": (
        "revenueGrowth", "quarterlyRevenueGrowth",
        "quarterlyrevenuegrowth.quarterly", "revenue_growth"
    ),
    "short_percent": (
        "shortPercentOfFloat", "sharesShortPercentOfFloat",
        "short_percentage_of_shares_outstanding.value", "short_percent"
    ),
    "avg_volume": ("averageDailyVolume3Month", "avgdailyvol3m", "avg_volume"),
}


def _metric(row: Mapping[str, Any], name: str) -> Optional[float]:
    return _number(_first(row, _ALIASES[name]))


def _normalise_saved_screen(frame: pd.DataFrame, category: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for rank, raw in enumerate(frame.to_dict("records"), start=1):
        symbol = str(_first(raw, _ALIASES["symbol"]) or "").strip().upper()
        if not symbol:
            continue

        pe = _metric(raw, "pe_ratio")
        forward_pe = _metric(raw, "forward_pe")
        peg = _metric(raw, "peg_ratio")
        eps_growth = _fraction(_first(raw, _ALIASES["eps_growth"]))
        revenue_growth = _fraction(_first(raw, _ALIASES["revenue_growth"]))
        short_percent = _fraction(_first(raw, _ALIASES["short_percent"]))

        if category == sources.CROWDED_SHORTS:
            score = short_percent if short_percent is not None else 1.0 / rank
            family = "high_short_interest"
        else:
            # The preset defines the gate; this score only gives the memory log
            # a continuous strength measure when the provider carries its inputs.
            value_wedge = max(0.0, 20.0 - pe) / 20.0 if pe is not None else 0.0
            peg_wedge = max(0.0, 1.0 - peg) if peg is not None else 0.0
            growth_wedge = max(0.0, eps_growth or 0.0)
            score = value_wedge + peg_wedge + growth_wedge + 1.0 / (100.0 + rank)
            family = (
                "large_cap_value" if category == sources.UNDERVALUED_LARGE_CAPS
                else "growth_at_discount"
            )

        row: Dict[str, Any] = {
            "symbol": symbol,
            "issuer": str(_first(raw, _ALIASES["issuer"]) or symbol),
            "family": family,
            "last_price": _metric(raw, "last_price"),
            "market_cap": _metric(raw, "market_cap"),
            "screen_rank": rank,
            "score": round(float(score), 4),
            "action": "investigate",
        }
        if category == sources.CROWDED_SHORTS:
            row.update({
                "short_percent": short_percent,
                "avg_volume": _metric(raw, "avg_volume"),
            })
        else:
            row.update({
                "pe_ratio": pe,
                "forward_pe": forward_pe,
                "peg_ratio": peg,
                "eps_growth": eps_growth,
                "revenue_growth": revenue_growth,
            })
        rows.append(row)
    return rows


def _record(namespace: str, rows: List[Dict[str, Any]], kind: str,
            parameters: Dict[str, Any], known_on: str) -> None:
    from ..thesis import memory

    payload_fields = (
        "family", "last_price", "market_cap", "pe_ratio", "forward_pe",
        "peg_ratio", "eps_growth", "revenue_growth", "short_percent",
        "avg_volume", "screen_rank", "one_month", "rsi14", "ma50_dist", "ma200_dist",
    )
    memory.record_events(
        family=namespace,
        rows=[{
            "symbol": row["symbol"],
            "known_on": known_on,
            "score": row.get("score"),
            "family": row.get("family"),
            "payload": {key: row.get(key) for key in payload_fields
                        if row.get(key) is not None},
        } for row in rows],
        kind=kind,
        parameters=parameters,
    )


def _saved_screen(preset: str, category: str, limit: int, provider: str) -> Result:
    limit = max(1, min(int(limit), 50))
    frame = yahoo.predefined_screen(preset, limit=limit)
    rows = _normalise_saved_screen(frame, category)[:limit]
    if not rows:
        raise EmptyDataError("{} returned no ticker-bearing rows".format(preset))
    as_of = str(datetime.now(timezone.utc).date())
    _record(category, rows, category, {"preset": preset, "limit": limit}, as_of)
    return Result(
        rows,
        provider=provider,
        warnings=[_ATTENTION_ONLY],
        extra={"preset": preset, "as_of": as_of, "category": category},
    )


@command("/thesis/undervalued_large_caps", providers=("yahoo",),
         summary="Large-cap value-screen candidates for thesis investigation")
def undervalued_large_caps(limit: int = 20,
                           provider: Optional[str] = None) -> Result:
    """US-listed $10B-$100B companies in Yahoo's low-P/E, sub-1 PEG screen.

    This is a valuation discrepancy queue, not a quality assertion.  The
    triage step must distinguish a genuine expectations gap from peak-cycle
    earnings, leverage, accounting noise or stale estimates.
    """
    src = resolve_provider(provider, ("yahoo",))
    return _saved_screen("undervalued_large_caps", sources.UNDERVALUED_LARGE_CAPS,
                         limit, src)


@command("/thesis/undervalued_growth", providers=("yahoo",),
         summary="Growth-at-a-discount candidates for thesis investigation")
def undervalued_growth(limit: int = 20,
                       provider: Optional[str] = None) -> Result:
    """US-listed companies in Yahoo's low-P/E, sub-1 PEG, high-EPS-growth screen.

    Growth and valuation fields can cover different periods.  Membership is a
    prompt to reconcile the denominator and durability in filings, not proof
    that growth is cheap.
    """
    src = resolve_provider(provider, ("yahoo",))
    return _saved_screen("undervalued_growth_stocks", sources.UNDERVALUED_GROWTH,
                         limit, src)


@command("/thesis/crowded_shorts", providers=("yahoo",),
         summary="Liquid stocks with the highest reported short crowding")
def crowded_shorts(limit: int = 20,
                   provider: Optional[str] = None) -> Result:
    """Liquid US stocks ranked by reported short percentage.

    The same row can seed a short thesis or a squeeze/reversal thesis.  The
    screen does not reveal borrow cost, age of the short-interest report,
    crowd composition or whether the shorts are paired hedges.
    """
    src = resolve_provider(provider, ("yahoo",))
    return _saved_screen("most_shorted_stocks", sources.CROWDED_SHORTS, limit, src)


@command("/thesis/price_dislocations", providers=("yahoo",),
         summary="Large one-month index-constituent drawdowns worth explaining")
def price_dislocations(index: str = "sp500", min_drop_pct: float = 12.0,
                       mcap_min: float = 2.0, limit: int = 20,
                       provider: Optional[str] = None) -> Result:
    """Largest one-month drawdowns in a supported index universe.

    ``min_drop_pct`` is an absolute percentage threshold and ``mcap_min`` is
    in $ billions.  The category is deliberately direction-neutral: a fall can
    be an overreaction, a newly visible impairment, or a factor/sector move.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_drop_pct = max(1.0, min(float(min_drop_pct), 80.0))
    mcap_min = max(0.0, min(float(mcap_min), 10_000.0))
    limit = max(1, min(int(limit), 50))

    # Reuse the platform's cached, batched universe build rather than issuing
    # one price request per constituent.
    from .screener import screener_run

    screen = screener_run(
        index=index,
        timeframe="one_month",
        direction="down",
        min_move=min_drop_pct,
        mcap_min=mcap_min,
        sort="one_month",
        ascending=True,
        limit=limit,
        provider=src,
    )
    rows = []
    for raw in list(screen.data or []):
        move = _number(raw.get("one_month"))
        rows.append({
            **raw,
            "symbol": str(raw.get("symbol") or "").upper(),
            "issuer": raw.get("name") or raw.get("symbol") or "?",
            "family": "one_month_drawdown",
            "score": round(abs(move), 4) if move is not None else None,
            "action": "investigate",
        })
    rows = [row for row in rows if row["symbol"]]
    if not rows:
        raise EmptyDataError(
            "No {} constituent fell at least {:.1f}% in one month".format(
                index, min_drop_pct)
        )

    as_of = str(screen.extra.get("as_of") or datetime.now(timezone.utc).date())
    params = {"index": index, "min_drop_pct": min_drop_pct,
              "mcap_min": mcap_min, "limit": limit}
    _record(sources.PRICE_DISLOCATIONS, rows, sources.PRICE_DISLOCATIONS,
            params, as_of)
    return Result(
        rows,
        provider=src,
        warnings=list(screen.warnings) + [_ATTENTION_ONLY],
        extra={**screen.extra, "category": sources.PRICE_DISLOCATIONS,
               "gate": params},
    )
