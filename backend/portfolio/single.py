"""One-name hedge simulation: the book is a single position in one stock.

The portfolio path hedges a diversified book against its benchmark, so its
shocks are benchmark windows and its candidates come from the index chain. A
single name inverts that. The tail being hedged is the name's *own* — mostly
idiosyncratic, which is exactly why an index hedge leaves most of it — so the
shock driver here is the stock's own return history and the instruments are
its own listed options. Everything downstream is the portfolio engine
unchanged: :func:`pricing.clean_chain`, the shock replay, the integer solve,
the cost table and the verdict all take this book as-is.

Sizing is in dollars because that is the shape of the question ("what would it
cost to protect $25,000 of this?"). Dollars floor to whole shares and whole
shares floor to whole contracts. Both roundings are reported rather than
smoothed away: a position under one contract cannot be optioned at all, and
saying so is the answer, not a failure to produce one.

No linear hedge is offered. Shorting the name against itself is not a hedge,
it is a sale — the engine already has a word for that, and the verdict says it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from . import candidates as candidates_module
from . import pricing

#: One listed option contract covers this many shares.
SHARES_PER_CONTRACT = 100

#: Share-count tolerance absorbing binary division error. Far below any
#: tradable fraction, so it can only ever recover a share, never invent one.
SHARE_EPSILON = 1e-9

#: A clicked call is an overwrite, not protection. It is priced and shown, but
#: this flag travels with it so no caller can present it as downside cover.
OVERWRITE_KINDS = ("covered_call",)


@dataclass(frozen=True)
class Position:
    """A long position in one name, sized from a dollar notional."""

    symbol: str
    shares: int
    spot: float
    closes: pd.Series
    notional_requested: float

    @property
    def market_value(self) -> float:
        return self.shares * self.spot

    @property
    def panel(self) -> pd.DataFrame:
        """The one-column holdings panel :func:`shocks.build_shocks` expects."""
        return pd.DataFrame({self.symbol: self.closes})

    @property
    def rows(self) -> List[Dict[str, Any]]:
        """Marked-to-market rows in the shape :func:`shocks.book_pnl` reads."""
        return [{"symbol": self.symbol, "market_value": self.market_value}]

    @property
    def contracts_covered(self) -> int:
        """Whole contracts this position can write against."""
        return self.shares // SHARES_PER_CONTRACT

    def sizing(self) -> Dict[str, Any]:
        """What the dollar ask became, including what the rounding cost."""
        note = None
        if self.contracts_covered < 1:
            note = (
                "{:,.0f} buys {} shares of {} — under the {} a contract covers, so no "
                "option can be sized against it. Raise the amount to at least {:,.0f} "
                "or de-risk by selling.".format(
                    self.notional_requested, self.shares, self.symbol,
                    SHARES_PER_CONTRACT, SHARES_PER_CONTRACT * self.spot,
                )
            )
        return {
            "symbol": self.symbol,
            "notional_requested": round(self.notional_requested, 2),
            "shares": self.shares,
            "spot": round(self.spot, 4),
            "market_value": round(self.market_value, 2),
            "uninvested_cash": round(self.notional_requested - self.market_value, 2),
            "contracts_covered": self.contracts_covered,
            "contract_notional": round(SHARES_PER_CONTRACT * self.spot, 2),
            "note": note,
        }


def position_from_notional(symbol: str, closes: pd.Series, notional: float) -> Position:
    """Turn a dollar ask into whole shares at the last close.

    Raises when the notional cannot buy a single share — there is no position
    to hedge, and inventing a fractional one would quietly change the question.
    """
    clean = closes.dropna()
    if clean.empty:
        raise ValueError("No price history for {}".format(symbol))
    spot = float(clean.iloc[-1])
    if spot <= 0:
        raise ValueError("{} last closed at {} — cannot size a position".format(symbol, spot))
    # Floor with a tolerance: exactly 100 shares' worth of a $305.93 stock
    # divides to 99.999999… in binary, and silently dropping that share would
    # cost the user their only contract.
    shares = int(math.floor(notional / spot + SHARE_EPSILON))
    if shares < 1:
        raise ValueError(
            "{:,.0f} does not buy one share of {} at {:,.2f}".format(notional, symbol, spot)
        )
    return Position(
        symbol=symbol.upper(),
        shares=shares,
        spot=spot,
        closes=clean,
        notional_requested=float(notional),
    )


def name_candidates(
    chain: pd.DataFrame,
    position: Position,
    as_of: date,
    horizon_sessions: int,
    instruments: Tuple[str, ...],
) -> Tuple[List[candidates_module.Candidate], List[Dict[str, str]]]:
    """Protective put, put spread and collar written on the name itself.

    The same builders the portfolio path uses on the index chain — nothing in
    them is index-specific — filtered to the instruments asked for.
    """
    built: List[candidates_module.Candidate] = []
    skipped: List[Dict[str, str]] = []

    if {"protective_put", "put_spread"} & set(instruments):
        option_candidates, option_skips = candidates_module.index_candidates(
            chain, position.symbol, position.spot, as_of, horizon_sessions
        )
        built.extend(c for c in option_candidates if c.structure.kind in instruments)
        skipped.extend(option_skips)

    if "collar" in instruments:
        collar, collar_skips = candidates_module.collar_candidate(
            chain, position.symbol, position.spot, position.shares, as_of, horizon_sessions
        )
        if collar is not None:
            built.append(collar)
        skipped.extend(collar_skips)

    return built, skipped


def contract_candidate(
    chain: pd.DataFrame,
    position: Position,
    as_of: date,
    expiration: str,
    strike: float,
    option_type: str,
    contracts: Optional[int] = None,
) -> Tuple[Optional[candidates_module.Candidate], List[Dict[str, str]]]:
    """One specific listed contract, used the only way it can hedge a long.

    A put is bought (protection, count solved for the target unless pinned). A
    call is *sold* against the shares — an overwrite, capped at the contracts
    the position covers, because writing more than you hold is a naked short,
    not a hedge. The kind is named accordingly so nothing downstream can
    present premium income as downside protection.
    """
    option_type = str(option_type).lower().rstrip("s")
    if option_type not in ("call", "put"):
        return None, [{"kind": "contract", "reason": "option_type must be call or put"}]

    cleaned, counts = pricing.clean_chain(chain, as_of)
    if cleaned.empty:
        return None, [
            {"kind": "contract", "reason": "no quote in this chain survived hygiene checks"}
        ]

    wanted = pd.to_datetime(expiration).date()
    match = cleaned[
        (cleaned["option_type"] == option_type)
        & (pd.to_datetime(cleaned["expiration"]).dt.date == wanted)
        & (pd.to_numeric(cleaned["strike"], errors="coerce").round(4) == round(float(strike), 4))
    ]
    if match.empty:
        return None, [
            {
                "kind": "contract",
                "reason": "{} {:g} {} has no quote that survives hygiene — zero bid, "
                "crossed, stale, or off the monotone surface".format(
                    wanted.isoformat(), float(strike), option_type
                ),
            }
        ]
    row = match.iloc[0]

    if option_type == "put":
        return (
            candidates_module.Candidate(
                structure=pricing.OptionStructure(
                    "protective_put",
                    position.symbol,
                    (pricing.OptionLeg.from_chain_row(row, 1),),
                ),
                liquidity=candidates_module._liquidity([row]),
                hygiene=counts,
                fixed_quantity=int(contracts) if contracts else None,
            ),
            [],
        )

    covered = position.contracts_covered
    if covered < 1:
        return None, [
            {
                "kind": "covered_call",
                "reason": "{} shares cover no whole contract — writing a call here "
                "would be a naked short, not an overwrite".format(position.shares),
            }
        ]
    quantity = min(int(contracts), covered) if contracts else covered
    return (
        candidates_module.Candidate(
            structure=pricing.OptionStructure(
                "covered_call",
                position.symbol,
                (pricing.OptionLeg.from_chain_row(row, -1),),
            ),
            liquidity=candidates_module._liquidity([row]),
            hygiene=counts,
            fixed_quantity=quantity,
        ),
        [],
    )
