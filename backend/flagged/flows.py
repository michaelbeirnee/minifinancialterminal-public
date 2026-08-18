"""Institutional-flow inflections at small caps: the flow measured against the tape.

A quarter-over-quarter change in reported 13F holdings is a sentiment reading
at a large cap — a few million shares of a name that trades fifty million a
day says something about a manager's view and nothing about the market. At a
small cap the same change *is* the market: a fund that cut a position from
twelve million shares to a quarter-million in a name that trades three hundred
thousand a day spent the quarter being most of the volume, and if it still
holds three million more it will spend part of the next one that way too. The
entry or exit is a liquidity event whose continuation is forecastable, and the
number that separates the two cases is the change divided by the average
daily volume.

So this module does one thing the raw flow table cannot: it puts a tape under
each flow. For every CUSIP with a change worth measuring it fetches the
quarter's daily volume, states the change as *days of volume*, and gates on
that — then adds what makes the row actionable rather than descriptive: how
much the net sellers still hold (the overhang, also in days), how much of the
gross flow was index managers rebalancing rather than anyone deciding
anything, and the market value that says "small cap" in the first place.

The dating is honest and it is late. A flow's ``known_on`` is the 45-day
filing deadline — the day the aggregate became knowable from EDGAR — and the
data set this reads lags that by another fortnight, so the freshest flow row
is always for the quarter before last. That is a property of the disclosure
regime, not of this code, and the row says which quarter it is.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

import re

from ..core.caching import TTL_FUNDAMENTAL, cached
from ..core.errors import EmptyDataError
from ..providers import sec, thirteenf
from . import INSTITUTIONAL_FLOW, row

# --------------------------------------------------------------------------- #
# Thresholds, with the reasoning they encode
# --------------------------------------------------------------------------- #
#: Days of average daily volume the net change must amount to. Five days is
#: a week of the whole tape: below it a position change is absorbed inside
#: ordinary trading and the "event" is a bookkeeping fact; above it the
#: managers involved were a visible share of every session in the quarter.
MIN_DAYS_OF_VOLUME = 5.0

#: "Small cap": market value at the period end, in dollars.
MAX_MARKET_CAP = 2_000_000_000
#: And a floor, because below it a name is a shell whose whole float is a
#: rounding error and whose volume is not a tape.
MIN_MARKET_CAP = 50_000_000

#: Dollar volume a day the name must trade for the flow to be tradeable at all
#: — a hundred days of volume in a stock that trades $30,000 a day is a fact
#: about illiquidity, not an opportunity.
MIN_DOLLAR_VOLUME = 250_000

#: The pre-gate before any volume is fetched: the change as a share of shares
#: outstanding. A quarter of a percent of the company is the least that could
#: be several days of volume at any turnover a small cap actually has, and it
#: cuts the universe from twenty-five thousand CUSIPs to a few hundred.
MIN_SHARE_OF_OUTSTANDING = 0.0025

#: How many candidates get a tape fetched, at most, per screen. One batched
#: request; past this the batch stops being one request.
MAX_PRICED = 400

#: Days of volume past which the score stops rising. Past a month and a half
#: of the tape the screen cannot tell two events apart on size, so it ranks
#: them by how much they still have left to do.
DAYS_CAP = 30.0

_SHARES_TAG = "EntityCommonStockSharesOutstanding"

#: Blank-check companies. Hedge funds accumulate SPAC shares for the trust
#: yield and the redemption option, in names that trade a few thousand shares
#: a day — every one of them is "sixty days of volume" and none of it is a
#: view on a business. Excluded from the screen by default, by name.
_SPAC = re.compile(r"\bACQUISITION\b|\bACQ\b|\bSPAC\b|\bBLANK\s+CHECK\b|\bMERGER\s+CORP\b", re.I)

#: A single filer holding more than this share of the company, and being most
#: of the flow, is more often a filing error than a position: one adviser's
#: information table scaled by a thousand puts it at 58% of a bank it has
#: never heard of. Anyone really at this level files a 13D. Labelled and
#: scored down rather than dropped, because Berkshire exists.
SINGLE_FILER_CEILING = 0.25

#: If the shares outstanding grew by at least this fraction of the net
#: accumulation between the two quarter ends — and by at least
#: :data:`ISSUANCE_MIN_DILUTION` of the company, so routine option and RSU
#: issuance does not qualify — the buyers were buying from the company: a
#: PIPE, a registered direct, an ATM. Those shares never crossed the tape,
#: and days-of-volume means nothing for them.
ISSUANCE_SHARE = 0.5
ISSUANCE_MIN_DILUTION = 0.02

#: Sessions of volume the name must have traded in the quarter. A stock that
#: listed in February has a tape of six weeks, and its "flow" is the IPO
#: allocation landing in 13F filings — not a market event and not divisible
#: by an average that does not exist yet.
MIN_SESSIONS = 50

#: Reported institutional shares above this fraction of shares outstanding
#: mean the two denominators disagree — the cover-page count is stale, or the
#: 13F is double-counting a shared position — and every ratio on the row is
#: built on sand. Labelled and scored down.
INSTITUTIONAL_CEILING = 1.10

#: Exchange-traded products. They have CUSIPs, they fail to deliver, a few
#: even file 10-Ks with a share count on the cover, and a 13F change in one is
#: asset allocation, not a view on a business.
_FUND = re.compile(
    r"\bETF\b|\bETN\b|\bFUND\b|\bISHARES\b|\bSPDR\b|\bPROSHARES\b|\bDIREXION\b|"
    r"\bVANECK\b|\b21SHARES\b|\bBITWISE\b|\bGRAYSCALE\b|"
    r"\b(?:BITCOIN|ETHER(?:EUM)?|XRP|SOLANA|CRYPTO)\b.*\bTRUST\b",
    re.I,
)


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else "{:+.1f}%".format(100 * value)


# --------------------------------------------------------------------------- #
# Reference: shares outstanding for the whole market, and CIK per symbol
# --------------------------------------------------------------------------- #
@cached("flagged.flows.shares_out.v2", ttl=TTL_FUNDAMENTAL)
def shares_outstanding(as_of: str) -> Dict[str, Tuple[float, str]]:
    """``cik -> (shares outstanding, domicile)`` from the cover pages, newest per filer.

    The domicile is the frame's ``loc`` ("US-IL", "CA-ON"), and it matters
    here because a Canadian or Israeli issuer's US line is often a fraction of
    its volume — the tape this module divides by is the US one, so days of
    volume at a dual listing is overstated by however much trades at home.

    The dei count is an instant on the cover of every 10-K and 10-Q, so the
    union of the last few quarterly frames covers nearly every domestic filer;
    the newest instant at or before ``as_of`` wins. Foreign private issuers
    do not tag it and fall out here — a small-cap screen loses some ADRs.
    """
    end = pd.Timestamp(as_of)
    frames: List[pd.DataFrame] = []
    year, quarter = end.year, (end.month - 1) // 3 + 1
    for back in range(5):
        y, q = year, quarter - back
        while q <= 0:
            q += 4
            y -= 1
        try:
            part = sec.frames(_SHARES_TAG, "CY{}Q{}I".format(y, q), taxonomy="dei", unit="shares")
        except Exception:  # noqa: BLE001 - a missing frame is a missing frame
            continue
        frames.append(part[["cik", "end", "val", "loc"]])
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    df["end"] = pd.to_datetime(df["end"], errors="coerce")
    df = df[(df["end"] <= end + pd.Timedelta(45, unit="D")) & (df["val"] > 0)]
    df = df.sort_values("end").drop_duplicates("cik", keep="last")
    return {str(c): (float(v), str(l or "")) for c, v, l in zip(df["cik"], df["val"], df["loc"])}


def _symbol_register() -> Tuple[Dict[str, str], set]:
    """``symbol -> cik`` and the set of symbols SEC lists, for reconciling."""
    table = sec.company_map()
    return dict(zip(table["symbol"], table["cik"])), set(table["symbol"])


def _reconcile(symbol: str, listed: set) -> str:
    """The FTD file writes ``BRKB``; SEC and Yahoo write ``BRK-B``."""
    if symbol in listed or len(symbol) < 2:
        return symbol
    dashed = symbol[:-1] + "-" + symbol[-1]
    return dashed if dashed in listed else symbol


# --------------------------------------------------------------------------- #
# The tape
# --------------------------------------------------------------------------- #
def _tape(symbols: Sequence[str], start: str, end: str) -> Dict[str, Dict[str, float]]:
    """Average daily volume and closing price over the window, per symbol.

    One batched download. A symbol with fewer than twenty sessions of volume
    in the quarter is left out — a name that traded on nine days has no
    average worth dividing by.
    """
    import yfinance as yf

    if not symbols:
        return {}
    data = yf.download(list(symbols), start=start, end=end, interval="1d",
                       auto_adjust=False, progress=False, threads=True,
                       group_by="column")
    if data is None or data.empty:
        return {}
    out: Dict[str, Dict[str, float]] = {}
    if isinstance(data.columns, pd.MultiIndex):
        volume = data["Volume"]
        close = data["Close"]
    else:
        volume = data[["Volume"]].rename(columns={"Volume": symbols[0]})
        close = data[["Close"]].rename(columns={"Close": symbols[0]})
    for sym in volume.columns:
        v = pd.to_numeric(volume[sym], errors="coerce").dropna()
        v = v[v > 0]
        c = pd.to_numeric(close[sym], errors="coerce").dropna() if sym in close.columns else pd.Series(dtype=float)
        if len(v) < 20 or c.empty:
            continue
        out[str(sym)] = {"adv": float(v.mean()), "sessions": int(len(v)),
                         "close": float(c.iloc[-1])}
    return out


def _quarter_start(period_end: str) -> str:
    end = pd.Timestamp(period_end)
    return pd.Timestamp(year=end.year, month=((end.month - 1) // 3) * 3 + 1, day=1).date().isoformat()


# --------------------------------------------------------------------------- #
# Gating — pure, so it can be tested on hand-built rows
# --------------------------------------------------------------------------- #
def gate(candidate: Dict[str, Any], *, min_days: float = MIN_DAYS_OF_VOLUME,
         max_market_cap: float = MAX_MARKET_CAP, min_market_cap: float = MIN_MARKET_CAP,
         min_dollar_volume: float = MIN_DOLLAR_VOLUME,
         direction: str = "any") -> Optional[Dict[str, Any]]:
    """Turn one priced candidate into a flag row, or ``None``.

    ``candidate`` carries the flow-table fields plus ``adv``, ``close``,
    ``shares_outstanding`` and ``symbol``. Everything the row states is
    computed here from those, so a test can hand in the numbers and check the
    verdict without touching SEC or Yahoo.
    """
    adv = candidate.get("adv") or 0.0
    close = candidate.get("close") or 0.0
    shares_out = candidate.get("shares_outstanding") or 0.0
    net = float(candidate.get("net_change") or 0.0)
    if adv <= 0 or close <= 0 or shares_out <= 0 or net == 0:
        return None
    if int(candidate.get("sessions") or 0) < MIN_SESSIONS:
        return None
    if _FUND.search(str(candidate.get("issuer") or "")):
        return None
    market_cap = close * shares_out
    if not (min_market_cap <= market_cap <= max_market_cap):
        return None
    dollar_volume = adv * close
    if dollar_volume < min_dollar_volume:
        return None
    days = abs(net) / adv
    if days < min_days:
        return None
    side = "accumulation" if net > 0 else "distribution"
    if direction != "any" and direction != side:
        return None

    sellers = candidate.get("top_sellers") or []
    buyers = candidate.get("top_buyers") or []
    movers = sellers if side == "distribution" else buyers

    # Issuance: the company sold the shares the "accumulators" bought.
    prior_out = candidate.get("shares_outstanding_prior")
    if prior_out is None or prior_out != prior_out or prior_out <= 0:   # None or NaN
        prior_out = None
    issued = (shares_out - prior_out) if prior_out else None
    issuance_suspected = bool(side == "accumulation" and issued is not None
                              and issued >= ISSUANCE_SHARE * net
                              and issued >= ISSUANCE_MIN_DILUTION * prior_out)
    # One filer, an implausible share of the company, most of the flow.
    top0 = movers[0] if movers else None
    single_filer_suspect = bool(
        top0 and abs(top0.get("change") or 0) >= 0.5 * abs(net)
        and max(top0.get("held_now") or 0, top0.get("held_prior") or 0) >= SINGLE_FILER_CEILING * shares_out
    )
    # The overhang: what the sellers still hold, in days of the same tape. It
    # is the forecastable half — an exit two-thirds done has a third to go.
    remaining = sum(int(m.get("held_now") or 0) for m in sellers)
    overhang_days = remaining / adv if adv else None
    top = movers[0] if movers else None
    top_share = abs(top["change"]) / abs(net) if top and net else None
    passive_share = float(candidate.get("passive_share") or 0.0)
    institutional_pct = float(candidate.get("shares_now") or 0.0) / shares_out
    denominator_suspect = institutional_pct > INSTITUTIONAL_CEILING
    domicile = str(candidate.get("domicile") or "")
    foreign = bool(domicile) and not domicile.upper().startswith("US")

    score = min(days, DAYS_CAP) / DAYS_CAP
    # Index rebalancing is arithmetic, not a decision; damp it rather than
    # hide it — the reader sees passive_share on the row. The two suspect
    # labels damp harder: they are the row saying "probably not a flow".
    score *= 1.0 - 0.5 * min(passive_share, 1.0)
    if issuance_suspected or single_filer_suspect or denominator_suspect:
        score *= 0.25
    if foreign:
        score *= 0.5

    summary = (
        "{} of {:,.0f} shares over the quarter = {:.1f} days of average volume "
        "({:,.0f}/day) at a ${:,.0f}M company{}{}".format(
            side, abs(net), days, adv, market_cap / 1e6,
            "; net sellers still hold {:.1f} days".format(overhang_days)
            if side == "distribution" and overhang_days else "",
            "; {:.0f}% of gross flow was index managers".format(100 * passive_share)
            if passive_share >= 0.25 else "")
        + ("; shares outstanding grew {:,.0f} — likely issuance, not the tape".format(issued)
           if issuance_suspected else "")
        + ("; one filer claims {:.0f}% of the company — check the filing".format(
            100 * max(top0.get("held_now") or 0, top0.get("held_prior") or 0) / shares_out)
           if single_filer_suspect else "")
        + ("; reported institutional shares are {:.0f}% of shares outstanding — the "
           "denominators disagree".format(100 * institutional_pct) if denominator_suspect else "")
        + ("; {} domicile — the US line may be a fraction of its volume".format(domicile)
           if foreign else "")
    )
    flag = row(
        INSTITUTIONAL_FLOW, candidate["symbol"], candidate["known_on"], summary,
        score=score,
        cusip=candidate.get("cusip"), issuer=candidate.get("issuer"),
        period_end=candidate.get("period_end"), prior_period_end=candidate.get("prior_period_end"),
        direction=side,
        net_change=int(net), shares_now=int(candidate.get("shares_now") or 0),
        shares_prior=int(candidate.get("shares_prior") or 0),
        gross_flow=int(candidate.get("gross_flow") or 0),
        filers_now=int(candidate.get("filers_now") or 0),
        filers_prior=int(candidate.get("filers_prior") or 0),
        positions_opened=int(candidate.get("positions_opened") or 0),
        positions_closed=int(candidate.get("positions_closed") or 0),
        entering_filer_shares=int(candidate.get("entering_filer_shares") or 0),
        departing_filer_shares=int(candidate.get("departing_filer_shares") or 0),
        adv_shares=round(adv), adv_dollars=round(dollar_volume),
        sessions=int(candidate.get("sessions") or 0),
        days_of_volume=round(days, 2),
        overhang_days=None if overhang_days is None else round(overhang_days, 2),
        sellers_remaining_shares=int(remaining),
        largest_mover=top["filer"] if top else None,
        largest_mover_share=None if top_share is None else round(top_share, 3),
        passive_share=round(passive_share, 4),
        shares_outstanding=round(shares_out),
        shares_outstanding_prior=None if not prior_out else round(prior_out),
        issuance_suspected=issuance_suspected,
        single_filer_suspect=single_filer_suspect,
        denominator_suspect=denominator_suspect,
        domicile=domicile or None, foreign_domicile=foreign,
        spac=bool(_SPAC.search(str(candidate.get("issuer") or ""))),
        institutional_pct=round(institutional_pct, 4),
        market_cap=round(market_cap),
        close=round(close, 4),
        top_buyers=buyers, top_sellers=sellers,
        data_set=candidate.get("data_set"),
    )
    # Accumulation and distribution are different bets; the base-rate report
    # splits on family, so they get their own.
    flag["family"] = "{}_{}".format(INSTITUTIONAL_FLOW, side)
    return flag


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #
def _candidates(current: Optional[Dict[str, Any]] = None,
                prior: Optional[Dict[str, Any]] = None) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """The flow table with a symbol, a CIK and shares outstanding attached."""
    if current is None or prior is None:
        current, prior = thirteenf.latest_pair()
    table = thirteenf.flows(current["url"], current["period_end"],
                            prior["url"], prior["period_end"])
    meta = {
        "period_end": current["period_end"], "prior_period_end": prior["period_end"],
        "known_on": current["deadline"], "data_set": current["file"],
        "cusips": int(len(table)),
        "common_filers": int(table["common_filers"].iloc[0]) if len(table) else 0,
    }
    table = table[~table["identity_change_suspected"]]
    meta["identity_changes_dropped"] = meta["cusips"] - int(len(table))

    cmap = thirteenf.cusip_symbol_map()
    by_cik, listed = _symbol_register()
    joined = table.merge(cmap[["cusip", "symbol"]], on="cusip", how="inner")
    joined["symbol"] = [_reconcile(s, listed) for s in joined["symbol"]]
    joined["cik"] = joined["symbol"].map(by_cik)
    meta["with_symbol"] = int(len(joined))

    outstanding = shares_outstanding(current["period_end"])
    outstanding_prior = shares_outstanding(prior["period_end"])
    joined["shares_outstanding"] = joined["cik"].map({k: v[0] for k, v in outstanding.items()})
    joined["domicile"] = joined["cik"].map({k: v[1] for k, v in outstanding.items()})
    joined["shares_outstanding_prior"] = joined["cik"].map({k: v[0] for k, v in outstanding_prior.items()})
    meta["with_shares_outstanding"] = int(joined["shares_outstanding"].notna().sum())
    joined = joined.dropna(subset=["shares_outstanding"])
    joined["change_share"] = joined["net_change"].abs() / joined["shares_outstanding"]
    joined["known_on"] = meta["known_on"]
    joined["data_set"] = meta["data_set"]
    return joined, meta


def screen(max_market_cap: float = MAX_MARKET_CAP, min_market_cap: float = MIN_MARKET_CAP,
           min_days: float = MIN_DAYS_OF_VOLUME, min_dollar_volume: float = MIN_DOLLAR_VOLUME,
           direction: str = "any", include_spacs: bool = False,
           include_suspect: bool = False,
           limit: int = 50) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Every small cap where last quarter's institutional flow was days of the tape.

    ``include_spacs`` keeps blank-check companies; ``include_suspect`` keeps
    rows the gate labelled as probable issuance or a single implausible filer.
    Both are off by default because a screen whose top twenty is SPAC trust
    arbitrage and PIPE closings has answered a different question.
    """
    joined, meta = _candidates()
    if not include_spacs:
        is_spac = joined["issuer"].fillna("").str.contains(_SPAC)
        meta["spacs_dropped"] = int(is_spac.sum())
        joined = joined[~is_spac]
    # Pre-gate before touching the tape: implied market value from the price
    # filers marked the position at, and the change as a share of the company.
    implied_cap = joined["implied_price"] * joined["shares_outstanding"]
    pool = joined[(implied_cap <= max_market_cap * 1.5)      # generous; the close decides
                  & (implied_cap >= min_market_cap * 0.5)
                  & (joined["change_share"] >= MIN_SHARE_OF_OUTSTANDING)]
    pool = pool.sort_values("change_share", ascending=False).head(MAX_PRICED)
    meta["priced"] = int(len(pool))
    if pool.empty:
        raise EmptyDataError("No 13F flow crossed the pre-gates for {}".format(meta["period_end"]))

    tape = _tape(pool["symbol"].tolist(), _quarter_start(meta["period_end"]),
                 (pd.Timestamp(meta["period_end"]) + pd.Timedelta(1, unit="D")).date().isoformat())
    meta["with_tape"] = len(tape)
    rows: List[Dict[str, Any]] = []
    for r in pool.to_dict("records"):
        t = tape.get(r["symbol"])
        if not t:
            continue
        flag = gate({**r, **t}, min_days=min_days, max_market_cap=max_market_cap,
                    min_market_cap=min_market_cap, min_dollar_volume=min_dollar_volume,
                    direction=direction)
        if not flag:
            continue
        if not include_suspect and (flag["issuance_suspected"] or flag["single_filer_suspect"]
                                    or flag["denominator_suspect"]):
            meta["suspect_dropped"] = meta.get("suspect_dropped", 0) + 1
            continue
        rows.append(flag)
    if not rows:
        raise EmptyDataError(
            "No small-cap 13F flow reached {} days of volume for {}".format(min_days, meta["period_end"]))
    # Rank: the score (capped days of volume, damped for index flow and for
    # a foreign domicile), then the overhang — what is left to happen — then
    # raw days. An exit two-thirds done at 40 days beats one finished at 60.
    rows.sort(key=lambda x: (-x["score"], -(x["overhang_days"] or 0), -x["days_of_volume"]))
    universe = int(len(pool))
    for position, r in enumerate(rows):
        r["screen_rank"] = position + 1
    meta["crossed_gates"] = len(rows)
    meta["returned"] = min(limit, len(rows))
    return rows[:limit], meta


def for_symbols(symbols: Sequence[str], min_days: float = 0.0,
                max_market_cap: float = float("inf"), min_market_cap: float = 0.0,
                min_dollar_volume: float = 0.0) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The flow row for specific symbols, gates relaxed — the per-company read.

    Where the market screen asks "which small caps had a flow this large", the
    per-symbol read asks "what was the flow here" and reports it whatever the
    size, so the gates default to nothing and the caller decides. A symbol
    with several CUSIPs (share classes) keeps the one most filers hold.
    """
    joined, meta = _candidates()
    wanted = {s.upper() for s in symbols}
    part = joined[joined["symbol"].isin(wanted)]
    if part.empty:
        return [], {**meta, "note": "none of {} appears in the 13F flow table with a "
                                    "mapped symbol and shares outstanding".format(sorted(wanted))}
    part = part.sort_values("filers_now", ascending=False).drop_duplicates("symbol")
    tape = _tape(part["symbol"].tolist(), _quarter_start(meta["period_end"]),
                 (pd.Timestamp(meta["period_end"]) + pd.Timedelta(1, unit="D")).date().isoformat())
    rows: List[Dict[str, Any]] = []
    for r in part.to_dict("records"):
        t = tape.get(r["symbol"])
        if not t:
            continue
        flag = gate({**r, **t}, min_days=min_days, max_market_cap=max_market_cap,
                    min_market_cap=min_market_cap, min_dollar_volume=min_dollar_volume)
        if flag:
            rows.append(flag)
    return rows, meta
