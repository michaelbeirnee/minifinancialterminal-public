"""Integer candidate solver and the normalized hedge cost table.

Step 4 of docs/hedge-construction.md. Takes what the earlier steps produced —
cleaned chains (step 2), the shock distribution (step 3), the book's P&L
array — and emits ranked, integer-sized hedge candidates with the corrected
cost decomposition:

* ``protection_bps`` — CVaR removed *before* cost, from the shock engine,
  with its bootstrap CI. Basis risk lives here (as less protection), never as
  a cost line: charging it twice was v1's bug.
* ``cost_bps`` — observable money only: bid–ask give-up at the touch plus the
  structure's decay to horizon in the no-shock state (plus carry for shorts).
  Forgone upside is its own column, not smuggled into cost.
* ``cost_per_unit_protection`` — the one comparable number; candidates are
  ranked on the CI's *lower* protection bound so a noisy estimate cannot win
  on optimism.

Sizing follows the per-instrument rules: puts and spreads solve for the
lowest integer contract count reaching the requested CVaR reduction; collars
are sized by shares covered; linear hedges arrive pre-sized from
beta-dollars. The small-book failure mode is a first-class verdict — when
hedging is infeasible at standard granularity or too expensive per unit of
protection, the honest output is "de-risk by selling", not the nearest bad
trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from . import pricing, shocks

#: Strike selection for the standard constructions, as fractions of spot.
PUT_MONEYNESS = 0.95
SPREAD_FLOOR_MONEYNESS = 0.80
COLLAR_FLOOR_MONEYNESS = 0.90
COLLAR_CAP_MONEYNESS = 1.10

#: The hedge must outlive the horizon with room to spare, or its value at
#: t+horizon is all pin risk.
TENOR_BUFFER_DAYS = 30

#: Above this cost per dollar of (lower-bound) protection, the verdict says
#: de-risk by selling instead. A named threshold, not a hidden one.
EXPENSIVE_RATIO = 0.5

#: Positive scenarios for the forgone-upside column (index moves at horizon).
UPSIDE_SCENARIOS = (0.10, 0.20)

#: Hard cap on the contract counts the solver will scan.
MAX_CONTRACTS = 200


@dataclass
class Candidate:
    """A priced structure plus the chain facts ranking needs."""

    structure: pricing.OptionStructure
    liquidity: Dict[str, Any]
    hygiene: Dict[str, int] = field(default_factory=dict)
    #: Collars carry their shares-covered quantity; None means "solve it".
    fixed_quantity: Optional[int] = None


# --------------------------------------------------------------------------- #
# Candidate construction from a cleaned chain
# --------------------------------------------------------------------------- #
def index_candidates(
    chain: pd.DataFrame,
    underlying: str,
    spot: float,
    as_of: date,
    horizon_sessions: int,
) -> Tuple[List[Candidate], List[Dict[str, str]]]:
    """Protective put and put spread on the index, from live chain rows.

    Chain hygiene is applied first; the expiry is the nearest one that
    outlives the horizon by :data:`TENOR_BUFFER_DAYS`. Returns candidates
    plus the reasons anything was skipped — an empty menu must say why.
    """
    cleaned, counts = pricing.clean_chain(chain, as_of)
    skipped: List[Dict[str, str]] = []
    min_days = (shocks.horizon_end(as_of, horizon_sessions) - as_of).days + TENOR_BUFFER_DAYS

    expiration = _pick_expiration(cleaned, as_of, min_days)
    if expiration is None:
        skipped.append(
            {
                "kind": "index_options",
                "reason": "no expiry at least {} days out survived quote hygiene".format(min_days),
            }
        )
        return [], skipped

    puts = cleaned[
        (cleaned["option_type"] == "put")
        & (pd.to_datetime(cleaned["expiration"]).dt.date == expiration)
    ]
    long_row = _nearest_strike(puts, PUT_MONEYNESS * spot)
    if long_row is None:
        skipped.append({"kind": "protective_put", "reason": "no usable put quotes at the expiry"})
        return [], skipped

    candidates = [
        Candidate(
            structure=pricing.OptionStructure(
                "protective_put",
                underlying.upper(),
                (pricing.OptionLeg.from_chain_row(long_row, 1),),
            ),
            liquidity=_liquidity([long_row]),
            hygiene=counts,
        )
    ]

    short_row = _nearest_strike(puts, SPREAD_FLOOR_MONEYNESS * spot)
    if short_row is not None and float(short_row["strike"]) < float(long_row["strike"]):
        candidates.append(
            Candidate(
                structure=pricing.OptionStructure(
                    "put_spread",
                    underlying.upper(),
                    (
                        pricing.OptionLeg.from_chain_row(long_row, 1),
                        pricing.OptionLeg.from_chain_row(short_row, -1),
                    ),
                ),
                liquidity=_liquidity([long_row, short_row]),
                hygiene=counts,
            )
        )
    else:
        skipped.append(
            {"kind": "put_spread", "reason": "no distinct floor strike below the long put"}
        )
    return candidates, skipped


def collar_candidate(
    chain: pd.DataFrame,
    underlying: str,
    spot: float,
    shares: float,
    as_of: date,
    horizon_sessions: int,
) -> Tuple[Optional[Candidate], List[Dict[str, str]]]:
    """A collar on a concentrated single name, sized by shares covered.

    Fewer than 100 shares is the granularity failure mode, reported as such
    rather than rounded up into an over-hedge.
    """
    quantity = int(shares // 100)
    if quantity < 1:
        return None, [
            {
                "kind": "collar",
                "reason": "{} holds {} shares — under one contract; de-risk by "
                "selling instead".format(underlying, int(shares)),
            }
        ]

    cleaned, counts = pricing.clean_chain(chain, as_of)
    min_days = (shocks.horizon_end(as_of, horizon_sessions) - as_of).days + TENOR_BUFFER_DAYS
    expiration = _pick_expiration(cleaned, as_of, min_days)
    if expiration is None:
        return None, [{"kind": "collar", "reason": "no usable expiry past the horizon"}]

    at_expiry = cleaned[pd.to_datetime(cleaned["expiration"]).dt.date == expiration]
    put_row = _nearest_strike(
        at_expiry[at_expiry["option_type"] == "put"], COLLAR_FLOOR_MONEYNESS * spot
    )
    call_row = _nearest_strike(
        at_expiry[at_expiry["option_type"] == "call"], COLLAR_CAP_MONEYNESS * spot
    )
    if put_row is None or call_row is None:
        return None, [{"kind": "collar", "reason": "floor or cap strike missing usable quotes"}]

    candidate = Candidate(
        structure=pricing.OptionStructure(
            "collar",
            underlying.upper(),
            (
                pricing.OptionLeg.from_chain_row(put_row, 1),
                pricing.OptionLeg.from_chain_row(call_row, -1),
            ),
        ),
        liquidity=_liquidity([put_row, call_row]),
        hygiene=counts,
        fixed_quantity=quantity,
    )
    return candidate, []


def _pick_expiration(cleaned: pd.DataFrame, as_of: date, min_days: int) -> Optional[date]:
    if cleaned.empty or "expiration" not in cleaned.columns:
        return None
    expirations = sorted(
        {stamp.date() for stamp in pd.to_datetime(cleaned["expiration"]).dropna()}
    )
    for expiration in expirations:
        if (expiration - as_of).days >= min_days:
            return expiration
    return None


def _nearest_strike(rows: pd.DataFrame, target: float) -> Optional[pd.Series]:
    if rows is None or rows.empty:
        return None
    distances = (pd.to_numeric(rows["strike"], errors="coerce") - target).abs()
    return rows.loc[distances.idxmin()]


def _number(value: Any) -> float:
    return 0.0 if value is None or pd.isna(value) else float(value)


def _liquidity(rows: Sequence[pd.Series]) -> Dict[str, Any]:
    """Worst-leg liquidity: a structure trades only as well as its worst leg."""
    open_interest = [_number(r.get("open_interest")) for r in rows]
    volume = [_number(r.get("volume")) for r in rows]
    spreads = []
    for r in rows:
        bid, ask = float(r["bid"]), float(r["ask"])
        mid = (bid + ask) / 2.0
        spreads.append((ask - bid) / mid if mid else np.inf)
    return {
        "min_open_interest": int(min(open_interest)),
        "min_volume": int(min(volume)),
        "max_relative_spread": round(float(max(spreads)), 4),
    }


# --------------------------------------------------------------------------- #
# The solver and the cost table
# --------------------------------------------------------------------------- #
def cost_table(
    shock_set: shocks.ShockSet,
    book_pnls: np.ndarray,
    value: float,
    candidates: Sequence[Candidate],
    spots: Dict[str, float],
    as_of: date,
    target_reduction: float,
    linear_hedges: Sequence[pricing.LinearHedge] = (),
    book_beta_dollars: Optional[float] = None,
    level: float = 0.05,
    rate: float = 0.0,
    div_yield: float = 0.0,
    short_carry_annual: float = 0.0,
    book_rows: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Rank every candidate on one comparable table, or say not to hedge.

    ``target_reduction`` is positive dollars of CVaR to remove at ``level``.
    Rows that reach it are ranked by cost per dollar of *lower-bound*
    protection; rows that cannot reach it stay in the table with their best
    achievable reduction and a status, so the menu never quietly shrinks.

    ``book_rows`` are the marked-to-market holdings. They are needed only to
    draw each row's display curve (:func:`shocks.scenario_curve`); without
    them the table is unchanged and simply carries no picture.
    """
    rows: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []

    for candidate in candidates:
        row = _option_row(
            candidate, shock_set, book_pnls, value, spots, as_of,
            target_reduction, book_beta_dollars, level, rate, div_yield,
            book_rows,
        )
        (rows if "reason" not in row else excluded).append(row)

    for hedge in linear_hedges:
        rows.append(
            _linear_row(
                hedge, shock_set, book_pnls, value, target_reduction,
                book_beta_dollars, level, short_carry_annual, as_of, book_rows,
            )
        )

    rows.sort(key=lambda r: r["rank_key"])
    for row in rows:
        row.pop("rank_key")
    return {
        "rows": rows,
        "excluded": excluded,
        "verdict": _verdict(rows, excluded, target_reduction, value),
        "assumptions": {
            "level": level,
            "rate": rate,
            "div_yield": div_yield,
            "short_carry_annual": short_carry_annual,
            "expensive_ratio": EXPENSIVE_RATIO,
            "protection_base": "model value today (execution give-up is in cost_bps)",
        },
    }


def _option_row(
    candidate: Candidate,
    shock_set: shocks.ShockSet,
    book_pnls: np.ndarray,
    value: float,
    spots: Dict[str, float],
    as_of: date,
    target_reduction: float,
    book_beta_dollars: Optional[float],
    level: float,
    rate: float,
    div_yield: float,
    book_rows: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    structure = candidate.structure
    spot = spots.get(structure.underlying.upper())
    if spot is None:
        return {"kind": structure.kind, "underlying": structure.underlying,
                "reason": "no spot price supplied"}

    entry = pricing.entry_cost(structure)
    mid = pricing.mid_cost(structure)
    if entry is None or mid is None:
        return {"kind": structure.kind, "underlying": structure.underlying,
                "reason": "missing executable quote on a leg — not priced on a mid fiction"}

    unit = shocks.hedge_unit_pnl(shock_set, structure, spot, as_of, rate, div_yield)

    if candidate.fixed_quantity is not None:
        quantity = candidate.fixed_quantity
        status = "sized_by_shares"
        curve = shocks.cvar_curve(book_pnls, unit, [quantity], level)
        reduction = curve[0]["cvar_reduction"]
        meets_target = reduction >= target_reduction
    else:
        status = "solved_for_target"
        max_quantity = min(MAX_CONTRACTS, max(1, math.ceil(2 * value / (100 * spot))))
        curve = shocks.cvar_curve(book_pnls, unit, range(0, max_quantity + 1), level)
        quantity, reduction, meets_target = _solve(curve, target_reduction)
    if quantity == 0:
        return {"kind": structure.kind, "underlying": structure.underlying,
                "reason": "no contract count adds protection against these shocks"}

    ci = shocks.protection_ci(book_pnls, unit, quantity, level)
    protection_bps = 10_000 * reduction / value
    protection_low = 10_000 * ci["cvar_reduction_ci95"][0] / value

    horizon_date = shocks.horizon_end(as_of, shock_set.horizon_sessions)
    base_value = pricing.structure_value(structure, spot, as_of, 0.0, rate, div_yield)
    neutral_value = pricing.structure_value(structure, spot, horizon_date, 0.0, rate, div_yield)
    spread_cost = quantity * (entry - mid)
    decay_cost = quantity * (base_value - neutral_value)
    cost_bps = 10_000 * (spread_cost + decay_cost) / value

    delta_dollars = sum(
        leg.quantity
        * leg.multiplier
        * pricing.bs_delta(
            spot, leg.strike, pricing.year_fraction(as_of, leg.expiration),
            leg.iv, rate, div_yield, leg.option_type,
        )
        * spot
        for leg in structure.legs
    )

    contract_notional = 100 * spot
    row = {
        "kind": structure.kind,
        "underlying": structure.underlying,
        "quantity": quantity,
        "status": status,
        "meets_target": meets_target,
        "legs": [
            {
                "option_type": leg.option_type,
                "strike": leg.strike,
                "expiration": leg.expiration.isoformat(),
                "quantity": leg.quantity * quantity,
            }
            for leg in structure.legs
        ],
        "protection_bps": round(protection_bps, 2),
        "protection_bps_ci95": [
            round(10_000 * bound / value, 2) for bound in ci["cvar_reduction_ci95"]
        ],
        "cost_bps": round(cost_bps, 2),
        "cost_breakdown": {
            "entry_cost": round(quantity * entry, 2),
            "bid_ask_give_up": round(spread_cost, 2),
            "decay_to_horizon": round(decay_cost, 2),
        },
        "cost_per_unit_protection": (
            round(cost_bps / protection_bps, 4) if protection_bps > 0 else None
        ),
        "over_under_hedge": round(reduction - target_reduction, 2),
        "delta_dollars": round(quantity * delta_dollars, 2),
        "upside_loss": _upside_loss(
            structure, quantity, spot, as_of, horizon_date, rate, div_yield, base_value
        ),
        "liquidity": candidate.liquidity,
        "quote_hygiene": candidate.hygiene,
        "rank_key": _rank_key(cost_bps, protection_low, meets_target),
    }
    if book_beta_dollars is not None:
        row["residual_beta_dollars"] = round(book_beta_dollars + quantity * delta_dollars, 2)
    if book_rows:
        def hedge_pnl(shock: float, iv_shift: float) -> float:
            """This many contracts, at horizon, net of the spread paid to get in."""
            shocked = pricing.structure_value(
                structure, spot * (1.0 + shock), horizon_date, iv_shift, rate, div_yield
            )
            return quantity * (shocked - base_value) - spread_cost

        row["scenario"] = _scenario(
            shock_set, book_rows, structure.underlying, hedge_pnl, horizon_date, level
        )
    if contract_notional > value:
        row["granularity_warning"] = (
            "one contract covers {:.0f} of a {:.0f} book — consider XSP, micro "
            "futures, or de-risking by selling".format(contract_notional, value)
        )
    return row


def _linear_row(
    hedge: pricing.LinearHedge,
    shock_set: shocks.ShockSet,
    book_pnls: np.ndarray,
    value: float,
    target_reduction: float,
    book_beta_dollars: Optional[float],
    level: float,
    short_carry_annual: float,
    as_of: date,
    book_rows: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """A pre-sized linear hedge on the same table as the options."""
    unit = hedge.notional * -shock_set.benchmark_return.to_numpy()
    reduction = shocks.cvar(book_pnls + unit, level) - shocks.cvar(book_pnls, level)
    ci = shocks.protection_ci(book_pnls, unit, 1, level)
    protection_bps = 10_000 * reduction / value
    protection_low = 10_000 * ci["cvar_reduction_ci95"][0] / value

    carry_cost = short_carry_annual * hedge.notional * shock_set.horizon_sessions / shocks.TRADING_DAYS
    cost_bps = 10_000 * carry_cost / value
    meets_target = reduction >= target_reduction

    row = {
        "kind": hedge.kind,
        "underlying": hedge.symbol,
        "notional": round(hedge.notional, 2),
        "status": "sized_by_beta_dollars",
        "meets_target": meets_target,
        "protection_bps": round(protection_bps, 2),
        "protection_bps_ci95": [
            round(10_000 * bound / value, 2) for bound in ci["cvar_reduction_ci95"]
        ],
        "cost_bps": round(cost_bps, 2),
        "cost_breakdown": {"carry_to_horizon": round(carry_cost, 2)},
        "cost_per_unit_protection": (
            round(cost_bps / protection_bps, 4) if protection_bps > 0 else None
        ),
        "over_under_hedge": round(reduction - target_reduction, 2),
        "upside_loss": {
            "{:+.0%}".format(s): round(hedge.pnl(s * hedge.beta), 2) for s in UPSIDE_SCENARIOS
        },
        "rank_key": _rank_key(cost_bps, protection_low, meets_target),
    }
    if book_beta_dollars is not None:
        row["residual_beta_dollars"] = round(
            book_beta_dollars - hedge.notional * hedge.beta, 2
        )
    if book_rows:
        # A short has no vol dimension and no curvature — the second argument
        # is accepted and ignored, which is exactly what a straight line means.
        row["scenario"] = _scenario(
            shock_set,
            book_rows,
            hedge.symbol,
            lambda shock, _iv_shift: hedge.pnl(shock) - carry_cost,
            shocks.horizon_end(as_of, shock_set.horizon_sessions),
            level,
        )
    return row


def _scenario(
    shock_set: shocks.ShockSet,
    book_rows: Sequence[Dict[str, Any]],
    underlying: str,
    hedge_pnl: Any,
    horizon_date: date,
    level: float,
) -> Dict[str, Any]:
    """The display curve for one sized row, plus what it is a curve *of*.

    Communication only, per the design doc: a payoff path has no
    probabilities, so it may illustrate the ranking above it and never
    contribute to it. The tail marker is the one probabilistic fact allowed
    in, and it is labelled as history rather than forecast.
    """
    exposure_rows, betas, label = _exposure(shock_set, book_rows, underlying)
    try:
        windows = shocks.underlying_windows(shock_set, underlying)
        tail_shock = round(float(np.quantile(windows, level)), 4)
    except ValueError:  # nothing to hedge against; the curve still draws
        tail_shock = None

    return {
        "underlying": underlying.upper(),
        "exposure": label,
        "exposure_value": round(sum(float(r["market_value"]) for r in exposure_rows), 2),
        "horizon_date": horizon_date.isoformat(),
        "tail_shock": tail_shock,
        "points": shocks.scenario_curve(shock_set, exposure_rows, hedge_pnl, betas),
        "basis": (
            "deterministic payoff at horizon, net of the spread paid — no "
            "probabilities, so it illustrates the ranking and never sets it"
        ),
    }


def _exposure(
    shock_set: shocks.ShockSet,
    book_rows: Sequence[Dict[str, Any]],
    underlying: str,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, float]], str]:
    """What the curve's unhedged line is: the whole book, or the one name.

    An index hedge covers everything, and everything moves beta × index. A
    hedge written on a holding puts *that name's* move on the x axis instead,
    so its beta against the index has already been spent getting there and
    must not be applied a second time.
    """
    key = underlying.upper()
    own = [r for r in book_rows if str(r["symbol"]).upper() == key]
    if own and not (key == shock_set.benchmark and len(own) != len(book_rows)):
        return own, None, key
    return list(book_rows), shock_set.betas, "portfolio"


def _solve(curve: List[Dict[str, Any]], target: float) -> Tuple[int, float, bool]:
    """Lowest integer count reaching the target, else the best achievable."""
    for point in curve:
        if point["quantity"] > 0 and point["cvar_reduction"] >= target:
            return point["quantity"], point["cvar_reduction"], True
    best = max(curve, key=lambda p: p["cvar_reduction"])
    return best["quantity"], best["cvar_reduction"], False


def _rank_key(cost_bps: float, protection_low_bps: float, meets_target: bool) -> Tuple[int, float]:
    """Sort key: candidates that do the job first, then cheapest per unit.

    Within each group the metric is cost per dollar of *lower-bound*
    protection, so a noisy estimate cannot win on optimism, and protection
    whose CI reaches zero ranks last outright. A credit structure (negative
    cost) legitimately sorts to the front of its group — its real price is
    the forgone upside, which is its own column, never folded into cost.
    """
    if protection_low_bps <= 0:
        return (1 if not meets_target else 0, float("inf"))
    return (0 if meets_target else 1, cost_bps / protection_low_bps)


def _upside_loss(
    structure: pricing.OptionStructure,
    quantity: int,
    spot: float,
    as_of: date,
    horizon_date: date,
    rate: float,
    div_yield: float,
    base_value: float,
) -> Dict[str, float]:
    """Hedge P&L in up markets — a real cost of collars, kept out of cost_bps."""
    out = {}
    for scenario in UPSIDE_SCENARIOS:
        value_up = pricing.structure_value(
            structure, spot * (1 + scenario), horizon_date, 0.0, rate, div_yield
        )
        out["{:+.0%}".format(scenario)] = round(quantity * (value_up - base_value), 2)
    return out


def _verdict(
    rows: List[Dict[str, Any]],
    excluded: List[Dict[str, str]],
    target_reduction: float,
    value: float,
) -> Dict[str, Any]:
    """The honest bottom line, including "don't hedge, sell"."""
    meeting = [r for r in rows if r.get("meets_target")]
    if not rows:
        return {
            "action": "de_risk_by_selling",
            "reason": "no candidate could be constructed or priced",
        }
    if not meeting:
        best = max(rows, key=lambda r: r["protection_bps"])
        return {
            "action": "de_risk_by_selling",
            "reason": "no candidate reaches the {:.0f} target; best achievable is "
            "{} ({} bps)".format(target_reduction, best["kind"], best["protection_bps"]),
        }
    best = meeting[0]
    ratio = best.get("cost_per_unit_protection")
    if ratio is not None and ratio > EXPENSIVE_RATIO:
        return {
            "action": "de_risk_by_selling",
            "reason": "cheapest qualifying hedge ({}) costs {:.2f} per dollar of "
            "protection — above the {:.2f} threshold".format(
                best["kind"], ratio, EXPENSIVE_RATIO
            ),
            "best_candidate": best["kind"],
        }
    return {
        "action": "hedge",
        "best_candidate": best["kind"],
        "size": (
            "{} contract{}".format(best["quantity"], "" if best["quantity"] == 1 else "s")
            if "quantity" in best
            else "{:,.0f} notional".format(best["notional"])
        ),
        "cost_per_unit_protection": ratio,
    }
