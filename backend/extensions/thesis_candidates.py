"""Additional market-wide funnels for the thesis generator.

Insider and congressional activity answer *who is acting*. These screens add
the other ways into an idea: what looks statistically cheap, where growth and
valuation disagree, what is growing fastest, what earns the most on its
capital, what converts that into cash, where profits are outrunning sales,
which balance sheets are under strain, where the short side is crowded, what
has already run, what pays a rising dividend, where price has moved far enough
to demand an explanation, and where sectors are separating from the market.
They remain attention signals. The shared triage and deep-dive layers decide
whether any row can become a falsifiable claim.

Every command normalises its provider-specific fields into a small stable row
shape and records the emitted rows in thesis memory.  That lets each category
earn (or fail to earn) a measured base rate instead of relying permanently on
the screen's intuition.

One property of the provider shapes everything below. Yahoo's screener does
not echo the fields it was filtered on: a screen gated on EBITDA growth answers
with an ordinary quote payload that has never carried it. So a funnel here
gates with the screen, then reads its own numbers back from the company
profile — see :func:`_hydrate` — and grades the rows only once those have
arrived. Scoring before that point does not fail; it silently produces an
inverted copy of the provider's ranking.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import pct_change_table
from ..providers import yahoo
from ..thesis import sources


_ATTENTION_ONLY = (
    "Screen membership is an attention signal, not a recommendation. Provider "
    "fundamentals, estimates and short-interest fields can be stale or use "
    "different reporting periods; verify the mechanism in filings and current "
    "market data before promoting a thesis."
)

_GROWTH_ATTENTION_ONLY = (
    "High reported growth is an attention signal, not a recommendation. "
    "Verify the comparison base, organic contribution, cash conversion, dilution, "
    "durability and valuation before promoting a thesis."
)

_SECTOR_ATTENTION_ONLY = (
    "Sector ETF leadership is an attention signal, not a recommendation. "
    "These funds are cap-weighted proxies: verify breadth, the causal driver, "
    "the catalyst and the condition that would disprove the thesis."
)

_QUALITY_ATTENTION_ONLY = (
    "A high return on equity is an attention signal, not a recommendation. "
    "Buybacks, intangible-light balance sheets and leverage all raise it "
    "without improving the business; verify the denominator, the durability of "
    "the margin and what is already in the price before promoting a thesis."
)

_CASH_ATTENTION_ONLY = (
    "Free cash flow yield is an attention signal, not a recommendation. The "
    "trailing figure can be flattered by deferred capital spending, working "
    "capital release or a disposal, and a high yield often prices a decline; "
    "verify maintenance capex, the cash conversion trend and the reason for "
    "the yield before promoting a thesis."
)

_STRESS_ATTENTION_ONLY = (
    "Balance-sheet stress is an attention signal, not a recommendation, and it "
    "is not a short signal — distress is frequently priced already. The Altman "
    "score was calibrated on manufacturers and misreads banks, insurers, REITs "
    "and asset-light software. Verify the maturity schedule, covenants and "
    "liquidity before promoting a thesis in either direction."
)

_MOMENTUM_ATTENTION_ONLY = (
    "Recent outperformance is an attention signal, not a recommendation. The "
    "screen selects on a move that has already happened, which is a survivorship "
    "filter rather than a forecast; verify what re-rated, whether earnings "
    "followed the price, and what would end it before promoting a thesis."
)

_REVISION_ATTENTION_ONLY = (
    "Estimate revisions are an attention signal, not a recommendation. Analysts "
    "re-base together after a print, so a burst of revisions is often one event "
    "counted many times, and the consensus follows the price at least as often "
    "as it leads it. Verify what changed and whether the move is already in the "
    "price before promoting a thesis."
)

_INCOME_ATTENTION_ONLY = (
    "A dividend-growth record is an attention signal, not a recommendation. A "
    "streak describes the past and a rising yield usually describes a falling "
    "price; verify payout coverage from free cash flow, the leverage funding it "
    "and the reason for the yield before promoting a thesis."
)


def _blank(value: Any) -> bool:
    """Is this provider field absent?

    ``NaN`` counts. A screen frame is a DataFrame, so a column one row lacks
    arrives as ``NaN`` rather than ``None`` in every other row — and ``NaN``
    is neither ``None`` nor ``""``, so it survives an emptiness test and comes
    out the far end as the issuer name "nan".
    """
    if value is None or value == "":
        return True
    return isinstance(value, float) and math.isnan(value)


def _first(row: Mapping[str, Any], names: Iterable[str]) -> Any:
    """First non-empty field, accepting Yahoo's several naming generations."""
    for name in names:
        value = row.get(name)
        if not _blank(value):
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
    # Trend fields the quote payload always carries, so they cost nothing.
    # Every distance below is a fraction — except the 52-week change, which
    # Yahoo states in percent. Reading it as a fraction puts a stock that rose
    # 3,844% at two cents a year ago, outside its own 52-week range.
    "one_year_change": ("fiftyTwoWeekChangePercent",),
    "high52_dist": ("fiftyTwoWeekHighChangePercent",),
    "low52_dist": ("fiftyTwoWeekLowChangePercent",),
    "ma50_dist": ("fiftyDayAverageChangePercent",),
    "ma200_dist": ("twoHundredDayAverageChangePercent",),
}

#: Trend fields the screen answers with directly; no profile request needed.
_TREND_FIELDS = ("one_year_change", "high52_dist", "low52_dist",
                 "ma50_dist", "ma200_dist")


def _metric(row: Mapping[str, Any], name: str) -> Optional[float]:
    return _number(_first(row, _ALIASES[name]))


def _scaled(value: Optional[float], scale: float) -> Optional[float]:
    return None if value is None else value * scale


def _count(value: Any) -> Optional[int]:
    """A tally, kept a tally. Providers hand these back as numpy floats."""
    number = _number(value)
    return None if number is None else int(number)


# --------------------------------------------------------------------------- #
# Profile hydration
# --------------------------------------------------------------------------- #
# Yahoo answers a screen filtered on ``epsgrowth.lasttwelvemonths`` with an
# ordinary quote payload, and that payload has never carried the field the
# screen was filtered on. Reading the gate back off the returned row is the
# natural mistake, and it does not fail loudly: it yields ``None`` forever. A
# growth funnel then prints "growth: revenue n/a · EPS n/a", a crowding funnel
# prints no crowding number, and the score quietly degenerates into an inverse
# of the provider's own rank. The card reaches the model with its one
# distinguishing fact missing.
#
# The profile endpoint does carry these, at one cached request per symbol.
# Units are per field and are not guessable. The profile states growth,
# margins, returns and short interest as fractions (``0.164`` is 16.4%), the
# dividend yield in percent, and debt-to-equity as a percentage of equity. A
# single "bigger than 1, so it must be a percent" heuristic reads a genuine
# 6,000%-growth micro-cap as 63% growth, so every field carries its own scale.
_PROFILE_NUMBERS: Dict[str, Tuple[Sequence[str], float]] = {
    "last_price": (("currentPrice", "regularMarketPrice"), 1.0),
    "market_cap": (("marketCap",), 1.0),
    "pe_ratio": (("trailingPE",), 1.0),
    "forward_pe": (("forwardPE",), 1.0),
    "peg_ratio": (("trailingPegRatio", "pegRatio"), 1.0),
    "eps_growth": (("earningsGrowth",), 1.0),
    "revenue_growth": (("revenueGrowth",), 1.0),
    "days_to_cover": (("shortRatio",), 1.0),
    "return_on_equity": (("returnOnEquity",), 1.0),
    "gross_margin": (("grossMargins",), 1.0),
    "operating_margin": (("operatingMargins",), 1.0),
    "profit_margin": (("profitMargins",), 1.0),
    "free_cash_flow": (("freeCashflow",), 1.0),
    "operating_cash_flow": (("operatingCashflow",), 1.0),
    "debt_to_equity": (("debtToEquity",), 0.01),
    "current_ratio": (("currentRatio",), 1.0),
    "dividend_yield": (("dividendYield",), 0.01),
    "payout_ratio": (("payoutRatio",), 1.0),
    "avg_volume": (("averageVolume", "averageDailyVolume3Month"), 1.0),
}

_PROFILE_TEXT: Dict[str, Sequence[str]] = {
    "sector": ("sector",),
    "industry": ("industry",),
    # A US listing quoted in dollars may report its accounts in another
    # currency. Whether these two agree decides whether a ratio built from one
    # statement figure and one market figure means anything at all.
    "quote_currency": ("currency",),
    "financial_currency": ("financialCurrency",),
}


def _short_of_float(raw: Mapping[str, Any]) -> Optional[float]:
    """Short interest as a share of float, computed rather than taken on trust.

    ``shortPercentOfFloat`` disagrees with Yahoo's own share counts often
    enough to be unusable as given: it reports one recent listing at 301% of
    float where the underlying counts say 29.8%, and an ADR at ten times its
    ratio-adjusted figure. Where the reported field is sane it matches the
    counts to four decimals, so the counts are what this trusts, and a reported
    value that survives as a fallback has to be physically possible first.
    """
    short = _number(raw.get("sharesShort"))
    floated = _number(raw.get("floatShares"))
    if short is not None and floated:
        return short / floated
    reported = _number(raw.get("shortPercentOfFloat"))
    return reported if reported is not None and 0.0 <= reported <= 1.5 else None


#: Profile facts that are computed from several fields rather than read from one.
_PROFILE_DERIVED: Dict[str, Callable[[Mapping[str, Any]], Optional[float]]] = {
    "short_percent": _short_of_float,
}


def _hydrate(rows: List[Dict[str, Any]], wanted: Sequence[str],
             max_workers: int = 8) -> int:
    """Fill in the fields the screen response does not carry. Returns rows filled.

    Only symbols actually missing something are fetched, so a screen that
    already answered costs nothing and a second run inside the profile cache
    costs nothing either. A profile that fails leaves its row exactly as the
    screen left it — thin, never wrong — because a funnel that cannot describe
    one candidate should still return the other nineteen.
    """
    wanted = tuple(name for name in wanted
                   if name in _PROFILE_NUMBERS or name in _PROFILE_TEXT
                   or name in _PROFILE_DERIVED)
    thin = [row for row in rows
            if any(row.get(name) is None for name in wanted) and row.get("symbol")]
    if not thin:
        return 0

    def _profile(symbol: str) -> Tuple[str, Mapping[str, Any]]:
        try:
            return symbol, yahoo.info(symbol)
        except Exception:  # noqa: BLE001 - one dead profile is not a dead scan
            return symbol, {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        profiles = dict(pool.map(_profile, sorted({row["symbol"] for row in thin})))

    filled = 0
    for row in thin:
        raw = profiles.get(row["symbol"]) or {}
        if not raw:
            continue
        before = sum(1 for name in wanted if row.get(name) is None)
        for name in wanted:
            if row.get(name) is not None:
                continue
            if name in _PROFILE_DERIVED:
                value = _PROFILE_DERIVED[name](raw)
                if value is not None:
                    row[name] = value
            elif name in _PROFILE_NUMBERS:
                aliases, scale = _PROFILE_NUMBERS[name]
                value = _number(_first(raw, aliases))
                if value is not None:
                    row[name] = value * scale
            else:
                value = _first(raw, _PROFILE_TEXT[name])
                if value:
                    row[name] = str(value)
        if before > sum(1 for name in wanted if row.get(name) is None):
            filled += 1
    return filled


def _normalise_screen(frame: pd.DataFrame) -> List[Dict[str, Any]]:
    """Provider screen rows, reduced to the shared shape every funnel emits.

    Only what the quote payload genuinely carries. The gate fields a custom
    screen filtered on are deliberately not invented here — they arrive later,
    from :func:`_hydrate`, or they stay ``None`` and the card says so.

    One issuer, one row. Yahoo's screener answers on listings, not companies,
    so a gate that catches Annaly catches its two preferred series as well and
    a gate that catches Alphabet catches both share classes. Each arrives with
    the same name and the same fundamentals, and each becomes an anomaly card
    the model reads as an independent candidate. The best-ranked listing keeps
    the slot.
    """
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for rank, raw in enumerate(frame.to_dict("records"), start=1):
        symbol = str(_first(raw, _ALIASES["symbol"]) or "").strip().upper()
        if not symbol:
            continue
        issuer = str(_first(raw, _ALIASES["issuer"]) or symbol)
        key = issuer.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "symbol": symbol,
            "issuer": issuer,
            "last_price": _metric(raw, "last_price"),
            "market_cap": _metric(raw, "market_cap"),
            "pe_ratio": _metric(raw, "pe_ratio"),
            "forward_pe": _metric(raw, "forward_pe"),
            "peg_ratio": _metric(raw, "peg_ratio"),
            "eps_growth": _fraction(_first(raw, _ALIASES["eps_growth"])),
            "revenue_growth": _fraction(_first(raw, _ALIASES["revenue_growth"])),
            "short_percent": _fraction(_first(raw, _ALIASES["short_percent"])),
            "avg_volume": _metric(raw, "avg_volume"),
            "one_year_change": _scaled(_metric(raw, "one_year_change"), 0.01),
            "high52_dist": _metric(raw, "high52_dist"),
            "low52_dist": _metric(raw, "low52_dist"),
            "ma50_dist": _metric(raw, "ma50_dist"),
            "ma200_dist": _metric(raw, "ma200_dist"),
            "screen_rank": rank,
            "action": "investigate",
        })
    return rows


def _trim(rows: List[Dict[str, Any]], keep: Sequence[str]) -> List[Dict[str, Any]]:
    """Reduce every row to this funnel's columns — the same columns on each row.

    Two properties, both of which the caller depends on. Columns this funnel
    never measured are dropped, because a column that is empty down its whole
    length reads as missing data rather than as an irrelevant question. And
    every row carries the same keys even where the value is missing, because
    the table is built from the first row's keys: a field that only the fourth
    candidate happens to have would otherwise have no column to appear in.
    """
    wanted = ("symbol", "issuer", "family", *keep, "score", "action")
    return [{name: row.get(name) for name in wanted} for row in rows]


#: Row keys the signal log already has columns for; everything else is payload.
_STRUCTURAL = frozenset({"symbol", "issuer", "family", "score", "action"})


def _record(namespace: str, rows: List[Dict[str, Any]], kind: str,
            parameters: Dict[str, Any], known_on: str) -> None:
    """Log what this scan emitted, so the category can earn a measured base rate.

    The payload is whatever the funnel actually emitted, minus the keys the log
    keeps its own columns for. It was an allowlist, which is the one shape that
    fails silently here: a newly registered source's distinguishing number is
    absent from a tuple written before that source existed, so it is dropped on
    the way into the log and simply never appears in the base-rate report.
    """
    from ..thesis import memory

    memory.record_events(
        family=namespace,
        rows=[{
            "symbol": row["symbol"],
            "known_on": known_on,
            "score": row.get("score"),
            "family": row.get("family"),
            "payload": {key: value for key, value in row.items()
                        if key not in _STRUCTURAL and value is not None},
        } for row in rows],
        kind=kind,
        parameters=parameters,
    )


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
# A score is a continuous strength measure for the signal log, not a ranking
# the model is invited to trust. Every one of these degrades to the provider's
# own ordering when the inputs are missing, and that tie-break term is kept
# deliberately tiny so a screen whose fields all failed to arrive scores near
# zero — visibly weak — rather than looking like a strong signal.
def _rank_tiebreak(row: Mapping[str, Any]) -> float:
    return 1.0 / (100.0 + float(row.get("screen_rank") or 1))


def _capped(value: Optional[float], ceiling: float = 1.0) -> float:
    """One term's bounded contribution to a score.

    Ratios arrive from the provider unbounded, and a company whose revenue went
    from $0.1m to $6m reports +6,000% growth. Left uncapped that one term owns
    the top of every list it lands in, and the ordering quietly becomes a search
    for the smallest denominator rather than the strongest signal. Past the
    ceiling the screen cannot tell two candidates apart, so it stops pretending
    to — the number itself still reaches the card in full.
    """
    if value is None:
        return 0.0
    return min(max(0.0, float(value)), ceiling)


def _value_score(row: Mapping[str, Any]) -> float:
    pe, peg = row.get("pe_ratio"), row.get("peg_ratio")
    return (
        (max(0.0, 20.0 - pe) / 20.0 if pe is not None else 0.0)
        + (max(0.0, 1.0 - peg) if peg is not None else 0.0)
        + _capped(row.get("eps_growth"))
        + _rank_tiebreak(row)
    )


def _growth_score(row: Mapping[str, Any]) -> float:
    # Direction is kept out of it. Fast growth can seed a long durability
    # thesis or a short expectations thesis, and the scanner does not choose.
    return (
        _capped(row.get("revenue_growth"))
        + _capped(row.get("eps_growth"))
        + _rank_tiebreak(row)
    )


def _finish(rows: List[Dict[str, Any]], *, category: str, limit: int,
            provider: str, params: Dict[str, Any], what: str,
            family: Any, score: Callable[[Mapping[str, Any]], Optional[float]],
            hydrate: Sequence[str] = (), keep: Sequence[str] = (),
            derive: Optional[Callable[[Dict[str, Any]], None]] = None,
            consider: Optional[int] = None,
            warning: str = _ATTENTION_ONLY, as_of: Optional[str] = None,
            extra: Optional[Dict[str, Any]] = None) -> Result:
    """The shared tail every funnel runs: hydrate, derive, grade, trim, log, answer.

    Grading happens *after* hydration on purpose. Scoring a row on fields the
    screen response never carried is how a strength measure turns into an
    inverted rank without anything appearing to go wrong.

    ``consider`` is how many of the screen's rows get graded, and defaults to
    the number returned. A funnel whose own ranking measure differs from the
    order Yahoo sorted by raises it: ranking free cash flow *yield* inside the
    twenty largest absolute cash generators only ever reorders the same twenty
    mega-caps, and never reaches the mid-cap the measure exists to find. Each
    extra row costs one cached profile request, so the widening is deliberate
    rather than free.
    """
    rows = rows[:max(limit, int(consider or limit))]
    if not rows:
        raise EmptyDataError(what)

    _hydrate(rows, hydrate)
    for row in rows:
        if derive is not None:
            derive(row)
        row["family"] = family(row) if callable(family) else family
        value = score(row)
        row["score"] = round(float(value), 4) if value is not None else 0.0
    rows.sort(key=lambda row: row["score"], reverse=True)
    rows = _trim(rows[:limit], keep)

    as_of = as_of or str(datetime.now(timezone.utc).date())
    _record(category, rows, category, params, as_of)
    return Result(
        rows,
        provider=provider,
        warnings=[warning],
        extra={"as_of": as_of, "category": category, "gate": params, **(extra or {})},
    )


def _saved_screen(preset: str, category: str, family: str, limit: int,
                  provider: str, score: Callable[[Mapping[str, Any]], Optional[float]],
                  keep: Sequence[str], hydrate: Sequence[str],
                  warning: str = _ATTENTION_ONLY) -> Result:
    limit = max(1, min(int(limit), 50))
    frame = yahoo.predefined_screen(preset, limit=limit)
    return _finish(
        _normalise_screen(frame), category=category, limit=limit, provider=provider,
        params={"preset": preset, "limit": limit},
        what="{} returned no ticker-bearing rows".format(preset),
        family=family, score=score, hydrate=hydrate, keep=keep, warning=warning,
        extra={"preset": preset},
    )


def _gated_screen(filters: List[List[Any]], category: str, family: Any, limit: int,
                  provider: str, sort_field: str,
                  score: Callable[[Mapping[str, Any]], Optional[float]],
                  keep: Sequence[str], hydrate: Sequence[str],
                  params: Dict[str, Any], what: str,
                  derive: Optional[Callable[[Dict[str, Any]], None]] = None,
                  consider: Optional[int] = None, sort_asc: bool = False,
                  warning: str = _ATTENTION_ONLY) -> Result:
    """A custom screen with explicit gates, rather than one of Yahoo's presets.

    The gates are stated in ``params`` and echoed back on the result, because a
    row's only claim to attention is the gate it passed and a caller reading the
    rows a week later cannot otherwise know what the gate was.
    """
    frame = yahoo.equity_screen(filters, limit=max(limit, int(consider or limit)),
                                sort_field=sort_field, sort_asc=sort_asc)
    return _finish(
        _normalise_screen(frame), category=category, limit=limit, provider=provider,
        params=params, what=what, family=family, score=score, consider=consider,
        hydrate=hydrate, keep=keep, derive=derive, warning=warning,
    )


#: Ordinary US listings. ADRs trade here too, which is a feature — but it means
#: a screen can return an issuer whose filings are 20-F rather than 10-K.
_US_LISTED = ["is-in", "exchange", "NMS", "NYQ"]

#: The valuation and growth fields the value screens describe themselves with.
_VALUE_FIELDS = ("last_price", "market_cap", "pe_ratio", "forward_pe",
                 "peg_ratio", "eps_growth", "revenue_growth", "sector",
                 "screen_rank")


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
                         "large_cap_value", limit, src, _value_score,
                         keep=_VALUE_FIELDS, hydrate=_VALUE_FIELDS)


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
                         "growth_at_discount", limit, src, _value_score,
                         keep=_VALUE_FIELDS, hydrate=_VALUE_FIELDS)


@command("/thesis/high_growth", providers=("yahoo",),
         summary="High revenue- and EPS-growth stocks for thesis investigation")
def high_growth(min_revenue_growth_pct: float = 20.0,
                min_eps_growth_pct: float = 20.0,
                min_market_cap_bn: float = 2.0,
                limit: int = 20,
                provider: Optional[str] = None) -> Result:
    """Liquid US high-growth companies across sectors, using explicit gates.

    The screen is deliberately direction-neutral: rapid reported growth can
    support a durability thesis or an expectations/deceleration short thesis.

    The gate is applied by Yahoo on its own fundamental fields; the growth
    numbers on each row are then read back from the company profile, because
    the screen answers with a quote payload that does not contain them.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_revenue_growth_pct = max(0.0, min(float(min_revenue_growth_pct), 200.0))
    min_eps_growth_pct = max(0.0, min(float(min_eps_growth_pct), 500.0))
    min_market_cap_bn = max(0.1, min(float(min_market_cap_bn), 1000.0))
    limit = max(1, min(int(limit), 50))
    return _gated_screen(
        [["gte", "quarterlyrevenuegrowth.quarterly", min_revenue_growth_pct],
         ["gte", "epsgrowth.lasttwelvemonths", min_eps_growth_pct],
         ["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000],
         _US_LISTED],
        sources.HIGH_GROWTH, "high_growth", limit, src,
        sort_field="quarterlyrevenuegrowth.quarterly",
        score=_growth_score, keep=_VALUE_FIELDS, hydrate=_VALUE_FIELDS,
        params={"min_revenue_growth_pct": min_revenue_growth_pct,
                "min_eps_growth_pct": min_eps_growth_pct,
                "min_market_cap_bn": min_market_cap_bn, "limit": limit},
        what="The high-growth screen returned no ticker-bearing rows",
        warning=_GROWTH_ATTENTION_ONLY,
    )


_SHORT_FIELDS = ("last_price", "market_cap", "short_percent", "days_to_cover",
                 "avg_volume", "sector", "screen_rank")


@command("/thesis/crowded_shorts", providers=("yahoo",),
         summary="Liquid stocks with the highest reported short crowding")
def crowded_shorts(min_short_percent: float = 15.0, min_days_to_cover: float = 3.0,
                   min_avg_volume: float = 500_000, min_market_cap_bn: float = 0.5,
                   limit: int = 20, provider: Optional[str] = None) -> Result:
    """Liquid US stocks ranked by reported short interest as a percent of float.

    The same row can seed a short thesis or a squeeze/reversal thesis.  The
    screen does not reveal borrow cost, age of the short-interest report,
    crowd composition or whether the shorts are paired hedges.

    Crowding without liquidity is a different phenomenon, so the gate is
    explicit rather than borrowed from Yahoo's "most shorted" preset — that
    preset is ordered by percentage alone and fills with nano-caps whose short
    interest is a handful of trades and whose float cannot absorb an exit.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_short_percent = max(1.0, min(float(min_short_percent), 90.0))
    min_days_to_cover = max(0.0, min(float(min_days_to_cover), 30.0))
    min_avg_volume = max(0.0, min(float(min_avg_volume), 50_000_000))
    min_market_cap_bn = max(0.05, min(float(min_market_cap_bn), 1000.0))
    limit = max(1, min(int(limit), 50))

    def family(row: Mapping[str, Any]) -> str:
        cover = row.get("days_to_cover")
        return ("hard_to_cover_short" if cover is not None and cover >= 10.0
                else "high_short_interest")

    def score(row: Mapping[str, Any]) -> float:
        return _capped(row.get("short_percent")) + _rank_tiebreak(row)

    return _gated_screen(
        [["gte", "short_percentage_of_float.value", min_short_percent],
         ["gte", "days_to_cover_short.value", min_days_to_cover],
         ["gte", "avgdailyvol3m", min_avg_volume],
         ["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000],
         _US_LISTED],
        sources.CROWDED_SHORTS, family, limit, src,
        sort_field="short_percentage_of_float.value",
        score=score, keep=_SHORT_FIELDS, hydrate=_SHORT_FIELDS,
        params={"min_short_percent": min_short_percent,
                "min_days_to_cover": min_days_to_cover,
                "min_avg_volume": min_avg_volume,
                "min_market_cap_bn": min_market_cap_bn, "limit": limit},
        what="No liquid US listing met the short-crowding gate",
    )


_QUALITY_FIELDS = ("last_price", "market_cap", "return_on_equity", "gross_margin",
                   "operating_margin", "debt_to_equity", "revenue_growth",
                   "pe_ratio", "forward_pe", "sector", "screen_rank")


@command("/thesis/quality_compounders", providers=("yahoo",),
         summary="High-return, high-margin, low-leverage businesses worth understanding")
def quality_compounders(min_return_on_equity_pct: float = 15.0,
                        min_gross_margin_pct: float = 40.0,
                        max_debt_to_equity_pct: float = 100.0,
                        min_market_cap_bn: float = 2.0,
                        limit: int = 20,
                        provider: Optional[str] = None) -> Result:
    """US listings earning a high return on equity at a high gross margin without leverage.

    The three gates are meant to be read together. Return on equity alone
    rewards a shrunken denominator, gross margin alone rewards an industry
    rather than a company, and either can be bought with debt — so the
    leverage ceiling is what makes the pair mean anything.

    This is a queue of businesses to understand, not of stocks to buy. Quality
    that is visible on a screen is quality the market has also seen.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_return_on_equity_pct = max(0.0, min(float(min_return_on_equity_pct), 200.0))
    min_gross_margin_pct = max(0.0, min(float(min_gross_margin_pct), 100.0))
    max_debt_to_equity_pct = max(0.0, min(float(max_debt_to_equity_pct), 1000.0))
    min_market_cap_bn = max(0.1, min(float(min_market_cap_bn), 5000.0))
    limit = max(1, min(int(limit), 50))

    def family(row: Mapping[str, Any]) -> str:
        leverage = row.get("debt_to_equity")
        return ("capital_efficient" if leverage is not None and leverage <= 0.5
                else "levered_return")

    def score(row: Mapping[str, Any]) -> float:
        return (_capped(row.get("return_on_equity"))
                + _capped(row.get("gross_margin"))
                + _rank_tiebreak(row))

    return _gated_screen(
        [["gte", "returnonequity.lasttwelvemonths", min_return_on_equity_pct],
         ["gte", "grossprofitmargin.lasttwelvemonths", min_gross_margin_pct],
         ["lte", "totaldebtequity.lasttwelvemonths", max_debt_to_equity_pct],
         ["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000],
         _US_LISTED],
        sources.QUALITY_COMPOUNDERS, family, limit, src,
        sort_field="returnonequity.lasttwelvemonths",
        score=score, keep=_QUALITY_FIELDS, hydrate=_QUALITY_FIELDS,
        params={"min_return_on_equity_pct": min_return_on_equity_pct,
                "min_gross_margin_pct": min_gross_margin_pct,
                "max_debt_to_equity_pct": max_debt_to_equity_pct,
                "min_market_cap_bn": min_market_cap_bn, "limit": limit},
        what="No US listing met the return, margin and leverage gates together",
        warning=_QUALITY_ATTENTION_ONLY,
    )


_CASH_FIELDS = ("last_price", "market_cap", "free_cash_flow", "fcf_yield",
                "operating_cash_flow", "profit_margin", "revenue_growth",
                "pe_ratio", "financial_currency", "sector", "screen_rank")


@command("/thesis/cash_generative", providers=("yahoo",),
         summary="Companies converting growth into free cash flow, ranked by FCF yield")
def cash_generative(min_free_cash_flow_mn: float = 200.0,
                    min_cash_flow_growth_pct: float = 5.0,
                    min_market_cap_bn: float = 2.0,
                    max_market_cap_bn: float = 100.0,
                    limit: int = 20,
                    provider: Optional[str] = None) -> Result:
    """US listings with substantial and growing levered free cash flow.

    Cash is the check on every other screen here: reported growth, reported
    margins and reported earnings are all accounting outputs, and free cash
    flow is the one that has to be paid for. The rows are ranked on free cash
    flow yield — the flow against the price being asked for it — which the
    screen cannot gate on directly and this command computes per row.

    Because Yahoo can only order by the absolute flow, the size ceiling is what
    makes the ranking mean anything: without one the pool is the handful of
    companies with the largest cash flows on earth, whose yields are low by
    construction, and the measure never reaches the mid-cap it exists to find.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_free_cash_flow_mn = max(0.0, min(float(min_free_cash_flow_mn), 100_000.0))
    min_cash_flow_growth_pct = max(-100.0, min(float(min_cash_flow_growth_pct), 500.0))
    min_market_cap_bn = max(0.1, min(float(min_market_cap_bn), 5000.0))
    max_market_cap_bn = max(min_market_cap_bn + 0.1,
                            min(float(max_market_cap_bn), 10_000.0))
    limit = max(1, min(int(limit), 50))

    def derive(row: Dict[str, Any]) -> None:
        # Two ways this arithmetic goes wrong, and both produce a confident
        # number rather than an error.
        #
        # Currency: the yield divides a statement figure by a market figure,
        # and a US listing may report its accounts in another currency. YPF is
        # quoted in dollars and reports in pesos, which turns a real ~6% yield
        # into 6,340%.
        #
        # Coherence: free cash flow is operating cash flow less capital
        # spending, so it cannot exceed operating cash flow. Where the
        # provider says it does, its two cash figures disagree and there is no
        # way to tell from here which one is wrong — a crypto trading firm
        # arrives claiming $34.6bn of free cash flow on $12.8bn of market value
        # while its operating cash flow is negative.
        #
        # In both cases the row keeps its cash numbers and simply has no yield,
        # which sinks it in the ranking. That is the right direction to fail:
        # a cash screen should not be topped by figures that contradict
        # themselves.
        flow, cap = row.get("free_cash_flow"), row.get("market_cap")
        operating = row.get("operating_cash_flow")
        reporting, quoted = row.get("financial_currency"), row.get("quote_currency")

        comparable = not (reporting and quoted) or reporting == quoted
        coherent = operating is None or flow is None or flow <= operating
        row["fcf_yield"] = None
        if comparable and coherent and flow is not None and cap:
            candidate = flow / cap
            if 0.0 < candidate <= 1.0:
                row["fcf_yield"] = candidate

    def family(row: Mapping[str, Any]) -> str:
        yield_ = row.get("fcf_yield")
        if yield_ is None:
            return "cash_generative"
        return "high_fcf_yield" if yield_ >= 0.06 else "cash_generative"

    def score(row: Mapping[str, Any]) -> float:
        # A 25% free cash flow yield is the top of the scale; past that the
        # number is usually telling you about a one-off, not a business.
        return _capped(row.get("fcf_yield"), 0.25) * 4.0 + _rank_tiebreak(row)

    return _gated_screen(
        [["gte", "leveredfreecashflow.lasttwelvemonths", min_free_cash_flow_mn * 1e6],
         ["gte", "cashfromoperations1yrgrowth.lasttwelvemonths", min_cash_flow_growth_pct],
         ["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000],
         ["lte", "intradaymarketcap", max_market_cap_bn * 1_000_000_000],
         _US_LISTED],
        sources.CASH_GENERATIVE, family, limit, src,
        sort_field="leveredfreecashflow.lasttwelvemonths",
        score=score, keep=_CASH_FIELDS,
        hydrate=_CASH_FIELDS + ("quote_currency",), derive=derive,
        # Yahoo can only sort on absolute cash flow; the yield that ranks these
        # rows is computed here, so the pool has to be wider than the answer.
        consider=min(limit * 2, 50),
        params={"min_free_cash_flow_mn": min_free_cash_flow_mn,
                "min_cash_flow_growth_pct": min_cash_flow_growth_pct,
                "min_market_cap_bn": min_market_cap_bn,
                "max_market_cap_bn": max_market_cap_bn, "limit": limit},
        what="No US listing met the free cash flow gate",
        warning=_CASH_ATTENTION_ONLY,
    )


_MARGIN_FIELDS = ("last_price", "market_cap", "operating_margin", "gross_margin",
                  "profit_margin", "revenue_growth", "eps_growth", "pe_ratio",
                  "forward_pe", "sector", "screen_rank")


@command("/thesis/margin_expansion", providers=("yahoo",),
         summary="Companies whose profits are growing much faster than their revenue")
def margin_expansion(min_ebitda_growth_pct: float = 25.0,
                     max_revenue_growth_pct: float = 15.0,
                     min_market_cap_bn: float = 2.0,
                     limit: int = 20,
                     provider: Optional[str] = None) -> Result:
    """US listings where EBITDA grew far faster than revenue over the last year.

    The wedge between the two is operating leverage — or it is a cost cut, a
    disposal, a change in what gets capitalised, or an easy comparison. Which
    one it is decides whether there is a thesis here at all, and the screen
    cannot tell them apart.

    Yahoo gates on its own EBITDA and revenue growth fields but does not return
    them, so each row carries the profile's revenue growth, earnings growth and
    current margins instead: the closest available read on the same question,
    not the gate itself.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_ebitda_growth_pct = max(0.0, min(float(min_ebitda_growth_pct), 500.0))
    max_revenue_growth_pct = max(-50.0, min(float(max_revenue_growth_pct), 100.0))
    min_market_cap_bn = max(0.1, min(float(min_market_cap_bn), 5000.0))
    limit = max(1, min(int(limit), 50))

    def score(row: Mapping[str, Any]) -> float:
        return (_capped(row.get("operating_margin"))
                + _capped(row.get("eps_growth"))
                + _rank_tiebreak(row))

    return _gated_screen(
        [["gte", "ebitda1yrgrowth.lasttwelvemonths", min_ebitda_growth_pct],
         ["lte", "totalrevenues1yrgrowth.lasttwelvemonths", max_revenue_growth_pct],
         ["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000],
         _US_LISTED],
        sources.MARGIN_EXPANSION, "operating_leverage", limit, src,
        sort_field="ebitda1yrgrowth.lasttwelvemonths",
        score=score, keep=_MARGIN_FIELDS, hydrate=_MARGIN_FIELDS,
        params={"min_ebitda_growth_pct": min_ebitda_growth_pct,
                "max_revenue_growth_pct": max_revenue_growth_pct,
                "min_market_cap_bn": min_market_cap_bn, "limit": limit},
        what="No US listing showed profit growth that far ahead of revenue growth",
    )


_STRESS_FIELDS = ("last_price", "market_cap", "debt_to_equity", "current_ratio",
                  "operating_margin", "profit_margin", "free_cash_flow",
                  "revenue_growth", "short_percent", "one_year_change",
                  "sector", "screen_rank")


@command("/thesis/balance_sheet_stress", providers=("yahoo",),
         summary="Leveraged balance sheets with weak solvency scores, for either side")
def balance_sheet_stress(max_altman_z: float = 1.8, min_debt_to_ebitda: float = 4.0,
                         min_market_cap_bn: float = 1.0, limit: int = 20,
                         provider: Optional[str] = None) -> Result:
    """US listings in the Altman distress zone carrying heavy debt against EBITDA.

    Deliberately direction-neutral, and the two directions are genuinely
    different theses: a balance sheet under strain is a short case if the
    strain is not priced and a recovery case if it is more than priced. The
    screen says nothing about which, and nothing about maturity walls,
    covenants, undrawn revolvers or refinancing already agreed — all of which
    decide whether pressure becomes an event.
    """
    src = resolve_provider(provider, ("yahoo",))
    max_altman_z = max(-10.0, min(float(max_altman_z), 10.0))
    min_debt_to_ebitda = max(0.0, min(float(min_debt_to_ebitda), 50.0))
    min_market_cap_bn = max(0.05, min(float(min_market_cap_bn), 1000.0))
    limit = max(1, min(int(limit), 50))

    def family(row: Mapping[str, Any]) -> str:
        liquidity = row.get("current_ratio")
        return ("distress_risk" if liquidity is not None and liquidity < 1.0
                else "levered_balance_sheet")

    def score(row: Mapping[str, Any]) -> float:
        leverage = _capped(row.get("debt_to_equity"), 3.0) / 3.0
        illiquidity = 1.0 - _capped(row.get("current_ratio"), 2.0) / 2.0
        return leverage + illiquidity + _rank_tiebreak(row)

    return _gated_screen(
        [["lte", "altmanzscoreusingtheaveragestockinformationforaperiod.lasttwelvemonths",
          max_altman_z],
         ["gte", "totaldebtebitda.lasttwelvemonths", min_debt_to_ebitda],
         ["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000],
         _US_LISTED],
        sources.BALANCE_SHEET_STRESS, family, limit, src,
        sort_field="altmanzscoreusingtheaveragestockinformationforaperiod.lasttwelvemonths",
        sort_asc=True,
        score=score, keep=_STRESS_FIELDS, hydrate=_STRESS_FIELDS,
        params={"max_altman_z": max_altman_z,
                "min_debt_to_ebitda": min_debt_to_ebitda,
                "min_market_cap_bn": min_market_cap_bn, "limit": limit},
        what="No US listing met the solvency and leverage gates together",
        warning=_STRESS_ATTENTION_ONLY,
    )


_MOMENTUM_FIELDS = ("last_price", "market_cap", "one_year_change", "high52_dist",
                    "low52_dist", "ma50_dist", "ma200_dist", "avg_volume",
                    "revenue_growth", "forward_pe", "sector", "screen_rank")


@command("/thesis/momentum_leaders", providers=("yahoo",),
         summary="Liquid stocks well up on the year and near their 52-week high")
def momentum_leaders(min_year_gain_pct: float = 30.0, max_high_distance_pct: float = 15.0,
                     min_avg_volume: float = 500_000, min_market_cap_bn: float = 2.0,
                     limit: int = 20, provider: Optional[str] = None) -> Result:
    """The mirror of the dislocation screen: moves up that also demand an explanation.

    A stock near its 52-week high after a large annual gain is a market
    disagreeing with an earlier price. The screen observes the disagreement
    and not its cause, which may be earnings revisions, a re-rating on the
    same earnings, an index or factor flow, or a story.

    ``max_high_distance_pct`` is applied after the screen, on the quote's own
    distance-from-high field, because Yahoo gates on the annual change but not
    on proximity to the high.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_year_gain_pct = max(0.0, min(float(min_year_gain_pct), 1000.0))
    max_high_distance_pct = max(0.0, min(float(max_high_distance_pct), 100.0))
    min_avg_volume = max(0.0, min(float(min_avg_volume), 50_000_000))
    min_market_cap_bn = max(0.1, min(float(min_market_cap_bn), 5000.0))
    limit = max(1, min(int(limit), 50))

    frame = yahoo.equity_screen(
        [["gte", "fiftytwowkpercentchange", min_year_gain_pct],
         ["gte", "avgdailyvol3m", min_avg_volume],
         ["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000],
         _US_LISTED],
        # Ask for more than is wanted: the proximity gate below is applied here
        # rather than by Yahoo, so some of what comes back will be dropped.
        limit=min(limit * 3, 50), sort_field="fiftytwowkpercentchange",
    )
    ceiling = -abs(max_high_distance_pct) / 100.0
    rows = [row for row in _normalise_screen(frame)
            if row.get("high52_dist") is not None and row["high52_dist"] >= ceiling]

    def family(row: Mapping[str, Any]) -> str:
        trend = row.get("ma200_dist")
        return ("extended_uptrend" if trend is not None and trend >= 0.25
                else "trending_leader")

    def score(row: Mapping[str, Any]) -> float:
        # Two halves: how far it has come, and how close it still is to the top
        # of its own range. A leader 1% off its high is a different row from
        # one that has already given back 15%.
        run = _capped(row.get("one_year_change"), 2.0) / 2.0
        proximity = max(0.0, 1.0 + (row.get("high52_dist") or 0.0))
        return run * 0.5 + proximity * 0.5 + _rank_tiebreak(row)

    return _finish(
        rows, category=sources.MOMENTUM_LEADERS, limit=limit, provider=src,
        params={"min_year_gain_pct": min_year_gain_pct,
                "max_high_distance_pct": max_high_distance_pct,
                "min_avg_volume": min_avg_volume,
                "min_market_cap_bn": min_market_cap_bn, "limit": limit},
        what="No liquid US listing was both up that much and that close to its high",
        family=family, score=score,
        keep=_MOMENTUM_FIELDS, hydrate=("revenue_growth", "forward_pe", "sector"),
        warning=_MOMENTUM_ATTENTION_ONLY,
    )


_INCOME_FIELDS = ("last_price", "market_cap", "dividend_yield", "payout_ratio",
                  "free_cash_flow", "revenue_growth", "eps_growth", "pe_ratio",
                  "one_year_change", "sector", "screen_rank")


@command("/thesis/dividend_growers", providers=("yahoo",),
         summary="Long unbroken dividend-growth records still paying a real yield")
def dividend_growers(min_growth_years: float = 10.0, min_forward_yield_pct: float = 2.0,
                     min_market_cap_bn: float = 2.0, limit: int = 20,
                     provider: Optional[str] = None) -> Result:
    """US listings with a long dividend-growth streak and a yield worth collecting.

    A streak is a fact about the past and a commitment about the future, and
    management treats breaking one as expensive — which is the mechanism worth
    testing, and also the reason a payout can outlive the cash that funded it.

    The two gates pull against each other on purpose: a streak is evidence of
    intent, while a yield that has become large is usually evidence the price
    fell. Each row carries the payout ratio and free cash flow so coverage can
    be checked rather than assumed.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_growth_years = max(1.0, min(float(min_growth_years), 60.0))
    min_forward_yield_pct = max(0.0, min(float(min_forward_yield_pct), 20.0))
    min_market_cap_bn = max(0.1, min(float(min_market_cap_bn), 5000.0))
    limit = max(1, min(int(limit), 50))

    def family(row: Mapping[str, Any]) -> str:
        payout = row.get("payout_ratio")
        return ("stretched_payout" if payout is not None and payout >= 0.8
                else "dividend_growth_streak")

    def score(row: Mapping[str, Any]) -> float:
        # Yield carries the ranking; an already-stretched payout discounts it,
        # because the yield is only worth what the cash behind it can sustain.
        payout = row.get("payout_ratio")
        coverage = 1.0 - _capped(payout, 1.0) * 0.5 if payout is not None else 1.0
        return _capped(row.get("dividend_yield"), 0.10) * 10.0 * coverage \
            + _rank_tiebreak(row)

    return _gated_screen(
        [["gte", "consecutive_years_of_dividend_growth_count", min_growth_years],
         ["gte", "forward_dividend_yield", min_forward_yield_pct],
         ["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000],
         _US_LISTED],
        sources.DIVIDEND_GROWERS, family, limit, src,
        sort_field="consecutive_years_of_dividend_growth_count",
        score=score, keep=_INCOME_FIELDS, hydrate=_INCOME_FIELDS,
        params={"min_growth_years": min_growth_years,
                "min_forward_yield_pct": min_forward_yield_pct,
                "min_market_cap_bn": min_market_cap_bn, "limit": limit},
        what="No US listing paired that dividend-growth record with that yield",
        warning=_INCOME_ATTENTION_ONLY,
    )


_REVISION_FIELDS = ("last_price", "market_cap", "analyst_count", "up_30d", "down_30d",
                    "net_revisions", "revision_breadth", "eps_drift_30d",
                    "eps_drift_90d", "consensus_eps_fy1", "one_year_change",
                    "high52_dist", "sector", "screen_rank")

#: The next full fiscal year — the row the sell side actually moves. The quarter
#: rows mostly track the last print; the out-year is where a changed view shows.
_HORIZON = "+1y"


def _estimate_row(symbol: str, kind: str) -> Mapping[str, Any]:
    """One horizon of one estimates table, or nothing. Never raises."""
    try:
        frame = yahoo.estimates(symbol, kind)
    except Exception:  # noqa: BLE001 - uncovered names are the normal case
        return {}
    if _HORIZON not in getattr(frame, "index", ()):
        return {}
    return dict(frame.loc[_HORIZON])


@command("/thesis/estimate_revisions", providers=("yahoo",),
         summary="Companies whose forward EPS consensus is moving, in either direction")
def estimate_revisions(min_net_revisions: int = 3,
                       min_estimate_drift_pct: float = 2.0,
                       min_analysts: int = 4,
                       min_market_cap_bn: float = 2.0,
                       min_avg_volume: float = 500_000,
                       limit: int = 20,
                       provider: Optional[str] = None) -> Result:
    """Liquid US listings whose next-year EPS consensus has moved, up or down.

    Every other funnel here screens on a *level* — cheap, high-margin, levered,
    up on the year, yielding four percent. This one screens on a *change in
    expectations*, which is a different question and the one most likely to be
    mid-flight rather than long since priced.

    Yahoo has no revision filter, so the gate cannot be pushed down to the
    screen: this builds a liquid universe, reads each name's revision counts
    and consensus history, and applies the gate here. That makes it the most
    expensive funnel on the menu on a cold cache — a couple of requests per
    candidate — and the universe size is a parameter for exactly that reason.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_net_revisions = max(1, min(int(min_net_revisions), 50))
    drift_floor = max(0.0, min(float(min_estimate_drift_pct), 100.0)) / 100.0
    min_analysts = max(1, min(int(min_analysts), 60))
    min_market_cap_bn = max(0.1, min(float(min_market_cap_bn), 5000.0))
    min_avg_volume = max(0.0, min(float(min_avg_volume), 50_000_000))
    limit = max(1, min(int(limit), 40))

    universe = min(max(limit * 5, 40), 200)
    frame = yahoo.equity_screen(
        [["gte", "intradaymarketcap", min_market_cap_bn * 1_000_000_000],
         ["gte", "avgdailyvol3m", min_avg_volume],
         _US_LISTED],
        limit=universe, sort_field="intradaymarketcap",
    )
    rows = _normalise_screen(frame)

    # Two reads per name decide whether it is even a candidate: how many desks
    # moved, and where the consensus went. Both are cached hard afterwards.
    def _movement(row: Dict[str, Any]) -> Dict[str, Any]:
        symbol = row["symbol"]
        counts = _estimate_row(symbol, "eps_revisions")
        trend = _estimate_row(symbol, "eps_trend")
        # Yahoo's own casing, which is not consistent across these columns.
        up_30d = _count(counts.get("upLast30days"))
        down_30d = _count(counts.get("downLast30days"))
        current = _number(trend.get("current"))

        def drift(ago: str) -> Optional[float]:
            was = _number(trend.get(ago))
            return current / was - 1.0 if current is not None and was else None

        row.update({
            "up_30d": up_30d, "down_30d": down_30d,
            "net_revisions": (up_30d - down_30d)
            if up_30d is not None and down_30d is not None else None,
            "eps_drift_30d": drift("30daysAgo"),
            "eps_drift_90d": drift("90daysAgo"),
            "consensus_eps_fy1": current,
        })
        return row

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(_movement, rows))

    def _moving(row: Mapping[str, Any]) -> bool:
        net, drift = row.get("net_revisions"), row.get("eps_drift_90d")
        return (net is not None and abs(net) >= min_net_revisions
                and drift is not None and abs(drift) >= drift_floor)

    rows = [row for row in rows if _moving(row)]

    # Coverage is only needed for the names that survived, and it is what turns
    # a raw count into a share of the desks following the company.
    def _coverage(row: Dict[str, Any]) -> Dict[str, Any]:
        count = _count(_estimate_row(row["symbol"], "earnings").get("numberOfAnalysts"))
        row["analyst_count"] = count
        net = row.get("net_revisions")
        row["revision_breadth"] = net / count if net is not None and count else None
        return row

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(_coverage, rows))
    rows = [row for row in rows
            if row.get("analyst_count") is not None
            and row["analyst_count"] >= min_analysts]

    def family(row: Mapping[str, Any]) -> str:
        net, drift = row.get("net_revisions"), row.get("eps_drift_90d")
        if net is None or drift is None:
            return "revisions_unclear"
        if net > 0 and drift >= 0.0:
            return "revisions_up"
        if net < 0 and drift <= 0.0:
            return "revisions_down"
        # The count and the consensus disagree, which is the interesting row:
        # desks are moving in one direction and the number in the other.
        return "revisions_mixed"

    def score(row: Mapping[str, Any]) -> float:
        breadth = abs(row.get("revision_breadth") or 0.0)
        drift = abs(row.get("eps_drift_90d") or 0.0)
        return (_capped(breadth) * 0.5 + _capped(drift / 0.25) * 0.5
                + _rank_tiebreak(row))

    return _finish(
        rows, category=sources.ESTIMATE_REVISIONS, limit=limit, provider=src,
        params={"min_net_revisions": min_net_revisions,
                "min_estimate_drift_pct": drift_floor * 100.0,
                "min_analysts": min_analysts,
                "min_market_cap_bn": min_market_cap_bn,
                "min_avg_volume": min_avg_volume, "limit": limit},
        what="No liquid US listing showed a one-sided revision run that large",
        family=family, score=score, keep=_REVISION_FIELDS, hydrate=("sector",),
        # Sorting on the drift, not on size: the screen only ordered the
        # universe, and the measure that selects these rows is computed here.
        consider=len(rows),
        extra={"universe": universe},
        warning=_REVISION_ATTENTION_ONLY,
    )


@command("/thesis/sector_rotation", providers=("yahoo",),
         summary="Sector ETF leaders and laggards versus the S&P 500")
def sector_rotation(min_relative_pct: float = 2.0, limit: int = 11,
                    provider: Optional[str] = None) -> Result:
    """Rank the 11 SPDR sector ETFs by absolute three-month return versus SPY.

    ``min_relative_pct`` only assigns leader / laggard families; all sectors
    remain visible so triage can reject apparent rotation that has no causal
    macro or fundamental mechanism. Returns are adjusted-price returns.
    """
    src = resolve_provider(provider, ("yahoo",))
    min_relative_pct = max(0.0, min(float(min_relative_pct), 25.0))
    limit = max(1, min(int(limit), 11))

    from .equity import compare_groups

    sector_screen = compare_groups(group="sector", provider=src)
    benchmark = pct_change_table(yahoo.history("SPY", period="10y")["close"])
    threshold = min_relative_pct / 100.0
    rows: List[Dict[str, Any]] = []
    for raw in list(sector_screen.data or []):
        relatives = {
            "relative_one_month": (
                raw.get("one_month") - benchmark.get("one_month")
                if raw.get("one_month") is not None and benchmark.get("one_month") is not None
                else None
            ),
            "relative_three_month": (
                raw.get("three_month") - benchmark.get("three_month")
                if raw.get("three_month") is not None and benchmark.get("three_month") is not None
                else None
            ),
            "relative_ytd": (
                raw.get("ytd") - benchmark.get("ytd")
                if raw.get("ytd") is not None and benchmark.get("ytd") is not None
                else None
            ),
            "relative_one_year": (
                raw.get("one_year") - benchmark.get("one_year")
                if raw.get("one_year") is not None and benchmark.get("one_year") is not None
                else None
            ),
        }
        focus = relatives["relative_three_month"]
        family = (
            "relative_leader" if focus is not None and focus >= threshold
            else "relative_laggard" if focus is not None and focus <= -threshold
            else "market_like"
        )
        rows.append({
            "symbol": str(raw.get("symbol") or "").upper(),
            "issuer": "{} sector".format(raw.get("group") or raw.get("symbol") or "?"),
            "sector": raw.get("group"),
            "family": family,
            "one_month": raw.get("one_month"),
            "three_month": raw.get("three_month"),
            "ytd": raw.get("ytd"),
            "one_year": raw.get("one_year"),
            **relatives,
            "score": round(abs(focus), 4) if focus is not None else 0.0,
            "action": "investigate",
        })
    rows = [row for row in rows if row["symbol"]]
    rows.sort(key=lambda row: row["score"], reverse=True)
    rows = rows[:limit]
    if not rows:
        raise EmptyDataError("No sector performance rows were available")

    as_of = str(datetime.now(timezone.utc).date())
    params = {"min_relative_pct": min_relative_pct, "limit": limit}
    _record(sources.SECTOR_ROTATION, rows, sources.SECTOR_ROTATION, params, as_of)
    return Result(
        rows,
        provider=src,
        warnings=list(sector_screen.warnings) + [_SECTOR_ATTENTION_ONLY],
        extra={"as_of": as_of, "benchmark": "SPY", "benchmark_returns": benchmark,
               "category": sources.SECTOR_ROTATION, "gate": params},
    )


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
