"""Option pricing, typed hedge instruments, and quote hygiene.

Step 2 of docs/hedge-construction.md. Everything here is a pure function of
its arguments — no fetching, no clocks (``as_of`` is always passed in) — so
the shock engine can replay these under thousands of shocks and the tests can
pin exact values offline.

Named approximations, disclosed rather than defaulted silently:

* European exercise (Black–Scholes with a continuous dividend yield). Fine
  for hedge sizing; SPY's early-exercise premium is ignored — XSP is the
  cleaner analysis vehicle.
* VIX options must be priced with :func:`black76_price` against the matching
  VIX future, never :func:`bs_price` against spot VIX.
* Executable prices: long legs cost the ask, short legs earn the bid. A leg
  without the needed quote makes the cost ``None`` — absence, not a guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import norm

#: Option tenor is measured in calendar days.
DAYS_PER_YEAR = 365.0

#: A quote whose last trade is older than this many calendar days is stale.
STALE_QUOTE_DAYS = 5

#: Mid-price monotonicity violations smaller than this are noise, not arb.
MONOTONE_TOLERANCE = 0.01


# --------------------------------------------------------------------------- #
# Pure pricing math
# --------------------------------------------------------------------------- #
def year_fraction(as_of: date, expiration: date) -> float:
    """Calendar-day tenor in years, floored at zero."""
    return max((expiration - as_of).days, 0) / DAYS_PER_YEAR


def intrinsic(spot: float, strike: float, option_type: str) -> float:
    """Exercise value — the price at expiry, no model needed."""
    if option_type == "call":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def bs_price(
    spot: float,
    strike: float,
    years: float,
    vol: float,
    rate: float = 0.0,
    div_yield: float = 0.0,
    option_type: str = "put",
) -> float:
    """Black–Scholes European price with continuous dividend yield.

    ``years <= 0`` collapses to intrinsic; ``vol <= 0`` collapses to the
    discounted deterministic-forward payoff — both are the correct limits, so
    the shock engine can march a contract straight through its expiry.
    """
    if years <= 0:
        return intrinsic(spot, strike, option_type)
    forward = spot * np.exp((rate - div_yield) * years)
    discount = np.exp(-rate * years)
    if vol <= 0:
        return discount * intrinsic(forward, strike, option_type)
    d1 = (np.log(spot / strike) + (rate - div_yield + vol**2 / 2) * years) / (
        vol * np.sqrt(years)
    )
    d2 = d1 - vol * np.sqrt(years)
    if option_type == "call":
        return float(
            spot * np.exp(-div_yield * years) * norm.cdf(d1)
            - strike * discount * norm.cdf(d2)
        )
    return float(
        strike * discount * norm.cdf(-d2)
        - spot * np.exp(-div_yield * years) * norm.cdf(-d1)
    )


def bs_delta(
    spot: float,
    strike: float,
    years: float,
    vol: float,
    rate: float = 0.0,
    div_yield: float = 0.0,
    option_type: str = "put",
) -> float:
    """Black–Scholes delta (per share of underlying)."""
    if years <= 0 or vol <= 0:
        step = 1.0 if spot > strike else 0.0
        return step if option_type == "call" else step - 1.0
    d1 = (np.log(spot / strike) + (rate - div_yield + vol**2 / 2) * years) / (
        vol * np.sqrt(years)
    )
    carry = np.exp(-div_yield * years)
    if option_type == "call":
        return float(carry * norm.cdf(d1))
    return float(carry * (norm.cdf(d1) - 1.0))


def black76_price(
    forward: float,
    strike: float,
    years: float,
    vol: float,
    rate: float = 0.0,
    option_type: str = "call",
) -> float:
    """Black-76 on a futures price — the correct model for VIX options."""
    if years <= 0:
        return intrinsic(forward, strike, option_type)
    discount = np.exp(-rate * years)
    if vol <= 0:
        return discount * intrinsic(forward, strike, option_type)
    d1 = (np.log(forward / strike) + vol**2 * years / 2) / (vol * np.sqrt(years))
    d2 = d1 - vol * np.sqrt(years)
    if option_type == "call":
        return float(discount * (forward * norm.cdf(d1) - strike * norm.cdf(d2)))
    return float(discount * (strike * norm.cdf(-d2) - forward * norm.cdf(-d1)))


# --------------------------------------------------------------------------- #
# Typed instruments
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptionLeg:
    """One option contract line inside a hedge structure.

    ``quantity`` is signed contracts: positive long, negative short.
    ``iv`` is the implied volatility the leg reprices with under shocks.
    """

    option_type: str
    strike: float
    expiration: date
    quantity: int
    iv: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    multiplier: int = 100
    contract_symbol: Optional[str] = None

    @classmethod
    def from_chain_row(cls, row: Any, quantity: int) -> "OptionLeg":
        """Build a leg from one (cleaned) chain row — dict or Series."""
        get = row.get if hasattr(row, "get") else row.__getitem__
        expiration = pd.Timestamp(get("expiration")).date()
        bid, ask = get("bid"), get("ask")
        return cls(
            option_type=str(get("option_type")),
            strike=float(get("strike")),
            expiration=expiration,
            quantity=quantity,
            iv=float(get("implied_volatility")),
            bid=float(bid) if bid is not None and not pd.isna(bid) else None,
            ask=float(ask) if ask is not None and not pd.isna(ask) else None,
            contract_symbol=get("contract_symbol"),
        )


@dataclass(frozen=True)
class OptionStructure:
    """A hedge candidate made of option legs on one underlying.

    ``kind`` names the construction ("protective_put", "put_spread",
    "collar", …) — the solver and the lifecycle log both key off it.
    """

    kind: str
    underlying: str
    legs: Tuple[OptionLeg, ...]


@dataclass(frozen=True)
class LinearHedge:
    """A short-futures / short-ETF hedge: pure linear beta removal.

    ``notional`` is positive dollars of the instrument sold short. Borrow,
    dividend and roll costs belong to the cost table, not the payoff.
    """

    kind: str
    symbol: str
    notional: float
    beta: float

    def pnl(self, instrument_return: float) -> float:
        return -self.notional * instrument_return


# --------------------------------------------------------------------------- #
# Structure valuation — what the shock engine replays
# --------------------------------------------------------------------------- #
def structure_value(
    structure: OptionStructure,
    spot: float,
    as_of: date,
    iv_shift: float = 0.0,
    rate: float = 0.0,
    div_yield: float = 0.0,
) -> float:
    """Model value of the structure in dollars at ``spot`` on ``as_of``.

    ``iv_shift`` is an additive level shift applied to every leg's own IV —
    the sticky-strike convention from the design doc. Legs past expiry are
    worth intrinsic; vol is floored so a large negative shift degrades to the
    deterministic limit instead of going negative.
    """
    total = 0.0
    for leg in structure.legs:
        years = year_fraction(as_of, leg.expiration)
        vol = max(leg.iv + iv_shift, 0.0)
        price = bs_price(spot, leg.strike, years, vol, rate, div_yield, leg.option_type)
        total += leg.quantity * leg.multiplier * price
    return total


def payoff_at_expiry(structure: OptionStructure, spot: float) -> float:
    """Intrinsic payoff of every leg — no model, per the design doc."""
    return sum(
        leg.quantity * leg.multiplier * intrinsic(spot, leg.strike, leg.option_type)
        for leg in structure.legs
    )


def entry_cost(structure: OptionStructure) -> Optional[float]:
    """Executable cost in dollars: long legs at the ask, short legs at the bid.

    Positive = premium paid. ``None`` when any leg is missing the quote its
    side needs — a structure that cannot be priced at the touch must not be
    ranked on a mid-price fiction (the "zero-cost collar" trap).
    """
    total = 0.0
    for leg in structure.legs:
        quote = leg.ask if leg.quantity > 0 else leg.bid
        if quote is None or quote <= 0:
            return None
        total += leg.quantity * leg.multiplier * float(quote)
    return total


def mid_cost(structure: OptionStructure) -> Optional[float]:
    """Mid-price cost, for showing the spread give-up next to entry_cost."""
    total = 0.0
    for leg in structure.legs:
        if leg.bid is None or leg.ask is None:
            return None
        total += leg.quantity * leg.multiplier * (leg.bid + leg.ask) / 2.0
    return total


# --------------------------------------------------------------------------- #
# Quote hygiene
# --------------------------------------------------------------------------- #
def _monotone_keep(mids, increasing: bool, tolerance: float = MONOTONE_TOLERANCE):
    """Positions forming the longest monotone run of mid prices.

    Keeping the longest run (rather than scanning greedily from the first
    strike) means one mispriced quote gets dropped by the majority around it
    instead of anchoring the filter and evicting every good quote after it.
    """
    n = len(mids)
    best, prev = [1] * n, [-1] * n
    for i in range(n):
        for j in range(i):
            ok = (
                mids[i] >= mids[j] - tolerance
                if increasing
                else mids[i] <= mids[j] + tolerance
            )
            if ok and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    if n == 0:
        return []
    end = max(range(n), key=lambda i: best[i])
    keep = []
    while end != -1:
        keep.append(end)
        end = prev[end]
    return sorted(keep)


def clean_chain(
    chain: pd.DataFrame,
    as_of: date,
    stale_days: int = STALE_QUOTE_DAYS,
) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """Filter a raw option chain to quotes a hedge may be built on.

    Drops, in order: zero/missing bids or asks, crossed markets, quotes whose
    last trade is older than ``stale_days``, and rows that break mid-price
    monotonicity in strike (calls must not rise, puts must not fall, beyond
    ``MONOTONE_TOLERANCE``). Full no-arbitrage surface checks are deliberately
    out of scope (design doc: filter the pragmatic subset, warn on the rest).

    Returns the kept rows and a count of what was dropped and why — silent
    truncation reads as "covered everything" when it didn't.
    """
    counts = {"zero_bid": 0, "crossed": 0, "stale": 0, "nonmonotonic": 0, "kept": 0}
    df = chain.copy()
    if "bid" not in df.columns or "ask" not in df.columns:
        counts["zero_bid"] = int(len(df))
        return df.iloc[0:0], counts

    bid = pd.to_numeric(df.get("bid"), errors="coerce")
    ask = pd.to_numeric(df.get("ask"), errors="coerce")
    quoted = (bid > 0) & (ask > 0)
    counts["zero_bid"] = int((~quoted).sum())
    df, bid, ask = df[quoted], bid[quoted], ask[quoted]

    uncrossed = bid <= ask
    counts["crossed"] = int((~uncrossed).sum())
    df = df[uncrossed]

    if "last_trade_date" in df.columns:
        traded = pd.to_datetime(df["last_trade_date"], errors="coerce")
        if getattr(traded.dt, "tz", None) is not None:
            traded = traded.dt.tz_localize(None)
        cutoff = pd.Timestamp(as_of) - timedelta(days=stale_days)
        fresh = traded >= cutoff
        counts["stale"] = int((~fresh).sum())
        df = df[fresh]

    keep_index = []
    for (_, option_type), group in df.groupby(["expiration", "option_type"]):
        group = group.sort_values("strike")
        mids = (
            (
                pd.to_numeric(group["bid"], errors="coerce")
                + pd.to_numeric(group["ask"], errors="coerce")
            )
            / 2.0
        ).tolist()
        kept = _monotone_keep(mids, increasing=option_type == "put")
        counts["nonmonotonic"] += len(group) - len(kept)
        keep_index.extend(group.index[position] for position in kept)
    df = df.loc[sorted(keep_index)]

    counts["kept"] = int(len(df))
    return df, counts
