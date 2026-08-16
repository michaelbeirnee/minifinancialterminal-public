"""Offline tests for the integer candidate solver and cost table.

Step 4 of docs/hedge-construction.md. Reuses the canned market from
test_shocks (AAA 2x the benchmark, BBB 0.5x) and adds canned chains, so
strike selection, integer solving, the cost decomposition, and the
"de-risk by selling" verdicts are all checked against known answers.
"""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from backend.portfolio import candidates, pricing, shocks
from tests.test_shocks import AS_OF, HORIZON, PANEL, ROWS, SPY, VIX

SPOT = float(SPY.iloc[-1])
FRESH = (AS_OF - timedelta(days=1)).isoformat()
NEAR_EXPIRY = (AS_OF + timedelta(days=40)).isoformat()   # inside tenor buffer
GOOD_EXPIRY = (AS_OF + timedelta(days=120)).isoformat()

BOOK_VALUE = 100_000.0
BETA_DOLLARS = 50_000.0 * 2.0 + 30_000.0 * 0.5  # AAA at 2x, BBB at 0.5x


def _put_row(moneyness, mid, expiration=GOOD_EXPIRY, **overrides):
    row = {
        "option_type": "put", "expiration": expiration,
        "strike": round(moneyness * SPOT), "bid": mid - 0.2, "ask": mid + 0.2,
        "implied_volatility": 0.22, "volume": 300, "open_interest": 900,
        "last_trade_date": FRESH, "contract_symbol": "SPY-P-{}".format(moneyness),
    }
    row.update(overrides)
    return row


def _spy_chain():
    rows = [
        _put_row(0.75, 1.6), _put_row(0.80, 2.5), _put_row(0.85, 4.0),
        _put_row(0.90, 6.5), _put_row(0.95, 10.5), _put_row(1.00, 16.0),
        _put_row(0.95, 8.0, expiration=NEAR_EXPIRY),
        _put_row(0.70, 1.0, bid=0.0),  # hygiene must catch this
    ]
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def shock_set():
    return shocks.build_shocks(PANEL, SPY, HORIZON, benchmark="SPY", vol_closes=VIX)


@pytest.fixture(scope="module")
def book(shock_set):
    return shocks.book_pnl(shock_set, ROWS)


# --------------------------------------------------------------------------- #
# Candidate construction
# --------------------------------------------------------------------------- #
def test_index_candidates_pick_tenor_and_strikes():
    built, skipped = candidates.index_candidates(_spy_chain(), "SPY", SPOT, AS_OF, HORIZON)
    kinds = [c.structure.kind for c in built]
    assert kinds == ["protective_put", "put_spread"]

    put = built[0].structure.legs[0]
    assert put.strike == round(0.95 * SPOT)
    assert put.expiration.isoformat() == GOOD_EXPIRY  # 40d expiry is too short

    spread = built[1].structure
    assert [leg.quantity for leg in spread.legs] == [1, -1]
    assert spread.legs[1].strike == round(0.80 * SPOT)

    assert built[0].hygiene["zero_bid"] == 1
    assert built[0].liquidity["min_open_interest"] == 900
    assert skipped == []


def test_short_tenor_chain_yields_no_candidates():
    chain = pd.DataFrame([_put_row(0.95, 8.0, expiration=NEAR_EXPIRY)])
    built, skipped = candidates.index_candidates(chain, "SPY", SPOT, AS_OF, HORIZON)
    assert built == []
    assert "no expiry" in skipped[0]["reason"]


def test_collar_is_sized_by_shares_covered():
    spot = 120.0
    chain = pd.DataFrame([
        {"option_type": "put", "expiration": GOOD_EXPIRY, "strike": 108,
         "bid": 3.0, "ask": 3.4, "implied_volatility": 0.3, "volume": 50,
         "open_interest": 200, "last_trade_date": FRESH},
        {"option_type": "call", "expiration": GOOD_EXPIRY, "strike": 132,
         "bid": 2.6, "ask": 3.0, "implied_volatility": 0.28, "volume": 40,
         "open_interest": 150, "last_trade_date": FRESH},
    ])
    collar, skipped = candidates.collar_candidate(chain, "AAA", spot, 250, AS_OF, HORIZON)
    assert skipped == []
    assert collar.fixed_quantity == 2
    assert {leg.option_type for leg in collar.structure.legs} == {"put", "call"}

    too_small, reasons = candidates.collar_candidate(chain, "AAA", spot, 80, AS_OF, HORIZON)
    assert too_small is None
    assert "de-risk by selling" in reasons[0]["reason"]


# --------------------------------------------------------------------------- #
# Solver and cost table
# --------------------------------------------------------------------------- #
def test_solver_finds_the_lowest_qualifying_count(shock_set, book):
    built, _ = candidates.index_candidates(_spy_chain(), "SPY", SPOT, AS_OF, HORIZON)
    protective = built[0]
    unit = shocks.hedge_unit_pnl(shock_set, protective.structure, SPOT, AS_OF)
    curve = shocks.cvar_curve(book, unit, [0, 1, 2, 3])
    # A target strictly between the 1- and 2-contract reductions must solve to 2.
    target = (curve[1]["cvar_reduction"] + curve[2]["cvar_reduction"]) / 2.0

    table = candidates.cost_table(
        shock_set, book, BOOK_VALUE, [protective], {"SPY": SPOT}, AS_OF,
        target_reduction=target, book_beta_dollars=BETA_DOLLARS,
    )
    row = table["rows"][0]
    assert row["quantity"] == 2
    assert row["meets_target"] is True
    assert row["over_under_hedge"] >= 0


def test_cost_table_decomposition_and_columns(shock_set, book):
    built, _ = candidates.index_candidates(_spy_chain(), "SPY", SPOT, AS_OF, HORIZON)
    linear = pricing.LinearHedge("short_etf", "SPY", notional=BETA_DOLLARS, beta=1.0)
    table = candidates.cost_table(
        shock_set, book, BOOK_VALUE, built, {"SPY": SPOT}, AS_OF,
        target_reduction=100.0, linear_hedges=[linear],
        book_beta_dollars=BETA_DOLLARS, short_carry_annual=0.005,
    )
    assert len(table["rows"]) == 3 and table["excluded"] == []

    by_kind = {r["kind"]: r for r in table["rows"]}
    put_row = by_kind["protective_put"]
    assert put_row["protection_bps"] > 0
    low, high = put_row["protection_bps_ci95"]
    assert low <= put_row["protection_bps"] <= high
    breakdown = put_row["cost_breakdown"]
    # Long put enters at the ask, 0.2 above mid: half the spread per share.
    assert breakdown["bid_ask_give_up"] == pytest.approx(
        put_row["quantity"] * 0.2 * 100, abs=1e-6
    )
    assert breakdown["decay_to_horizon"] > 0          # long options bleed theta
    assert put_row["delta_dollars"] < 0               # it is a short-delta hedge
    assert put_row["residual_beta_dollars"] < BETA_DOLLARS
    assert all(v < 0 for v in put_row["upside_loss"].values())
    assert "rank_key" not in put_row

    linear_row = by_kind["short_etf"]
    assert linear_row["residual_beta_dollars"] == pytest.approx(0.0, abs=1e-6)
    assert linear_row["cost_bps"] > 0                 # borrow carry is charged
    assert linear_row["upside_loss"]["+10%"] < 0      # symmetric: upside surrendered

    verdict = table["verdict"]
    if verdict["action"] == "hedge":
        assert "best_candidate" in verdict
    else:
        assert "reason" in verdict


def test_unreachable_target_recommends_selling(shock_set, book):
    built, _ = candidates.index_candidates(_spy_chain(), "SPY", SPOT, AS_OF, HORIZON)
    table = candidates.cost_table(
        shock_set, book, BOOK_VALUE, built, {"SPY": SPOT}, AS_OF,
        target_reduction=10_000_000.0,
    )
    assert all(r["meets_target"] is False for r in table["rows"])
    assert table["verdict"]["action"] == "de_risk_by_selling"
    assert "no candidate reaches" in table["verdict"]["reason"]


def test_unpriceable_leg_is_excluded_not_mid_priced(shock_set, book):
    legs = (
        pricing.OptionLeg("put", round(0.95 * SPOT), pd.Timestamp(GOOD_EXPIRY).date(),
                          1, 0.22, bid=10.3, ask=10.7),
        pricing.OptionLeg("put", round(0.80 * SPOT), pd.Timestamp(GOOD_EXPIRY).date(),
                          -1, 0.22, bid=None, ask=2.7),
    )
    broken = candidates.Candidate(
        structure=pricing.OptionStructure("put_spread", "SPY", legs), liquidity={},
    )
    table = candidates.cost_table(
        shock_set, book, BOOK_VALUE, [broken], {"SPY": SPOT}, AS_OF, target_reduction=100.0,
    )
    assert table["rows"] == []
    assert "mid fiction" in table["excluded"][0]["reason"]


def test_small_book_gets_the_granularity_warning(shock_set):
    small_rows = [
        {"symbol": "AAA", "market_value": 12_000.0},
        {"symbol": "BBB", "market_value": 8_000.0},
    ]
    small_book = shocks.book_pnl(shock_set, small_rows)
    built, _ = candidates.index_candidates(_spy_chain(), "SPY", SPOT, AS_OF, HORIZON)
    table = candidates.cost_table(
        shock_set, small_book, 20_000.0, [built[0]], {"SPY": SPOT}, AS_OF,
        target_reduction=50.0,
    )
    row = table["rows"][0]
    assert "granularity_warning" in row
    assert "XSP" in row["granularity_warning"]


def test_qualifying_candidates_rank_above_cheaper_non_qualifying(shock_set, book):
    """A credit structure that misses the goal must not outrank one that meets it.

    Collars often carry a negative cost (the short call funds the put), which
    would sort them to the top on cost-per-protection alone even when they do
    not reach the target. Doing the job comes first; price breaks ties.
    """
    built, _ = candidates.index_candidates(_spy_chain(), "SPY", SPOT, AS_OF, HORIZON)
    protective = built[0]
    unit = shocks.hedge_unit_pnl(shock_set, protective.structure, SPOT, AS_OF)
    # Reachable by scaling puts to 5 contracts, out of reach for a 1-lot collar.
    reachable = shocks.cvar_curve(book, unit, [5])[0]["cvar_reduction"]

    # A credit collar sized so it cannot reach the target.
    credit_legs = (
        pricing.OptionLeg("put", round(0.80 * SPOT), pd.Timestamp(GOOD_EXPIRY).date(),
                          1, 0.22, bid=1.0, ask=1.2),
        pricing.OptionLeg("call", round(1.10 * SPOT), pd.Timestamp(GOOD_EXPIRY).date(),
                          -1, 0.22, bid=6.0, ask=6.4),
    )
    credit = candidates.Candidate(
        structure=pricing.OptionStructure("collar", "SPY", credit_legs),
        liquidity={"min_open_interest": 500, "min_volume": 100, "max_relative_spread": 0.05},
        fixed_quantity=1,
    )

    table = candidates.cost_table(
        shock_set, book, BOOK_VALUE, [protective, credit], {"SPY": SPOT}, AS_OF,
        target_reduction=reachable * 0.9,
    )
    top = table["rows"][0]
    assert top["meets_target"] is True
    assert top["kind"] == "protective_put"
    collar_row = next(r for r in table["rows"] if r["kind"] == "collar")
    assert collar_row["cost_bps"] < top["cost_bps"]     # cheaper on the ratio...
    assert collar_row["meets_target"] is False          # ...but it does not do the job


# --------------------------------------------------------------------------- #
# Display curve on the rows (docs/hedge-construction.md: communicates, never ranks)
# --------------------------------------------------------------------------- #
def _table(shock_set, book, **kwargs):
    built, _ = candidates.index_candidates(_spy_chain(), "SPY", SPOT, AS_OF, HORIZON)
    return candidates.cost_table(
        shock_set, book, BOOK_VALUE, built, {"SPY": SPOT}, AS_OF,
        target_reduction=2_000.0,
        linear_hedges=[pricing.LinearHedge("short_etf", "SPY", BETA_DOLLARS, 1.0)],
        book_beta_dollars=BETA_DOLLARS,
        **kwargs,
    )


def test_rows_carry_a_curve_only_when_the_book_is_supplied(shock_set, book):
    without = _table(shock_set, book)
    assert all("scenario" not in row for row in without["rows"])

    with_book = _table(shock_set, book, book_rows=ROWS)
    assert all("scenario" in row for row in with_book["rows"])
    # Ranking is the shock engine's; drawing a picture must not touch it.
    assert [r["kind"] for r in with_book["rows"]] == [r["kind"] for r in without["rows"]]
    assert [r["protection_bps"] for r in with_book["rows"]] == \
           [r["protection_bps"] for r in without["rows"]]


def test_the_curve_shows_where_a_put_spread_stops_protecting(shock_set, book):
    """The one thing a table of averages cannot say, and the picture can."""
    table = _table(shock_set, book, book_rows=ROWS)
    spread = next(r for r in table["rows"] if r["kind"] == "put_spread")
    put = next(r for r in table["rows"] if r["kind"] == "protective_put")

    def at(row, shock):
        return next(p for p in row["scenario"]["points"] if p["shock"] == shock)

    # The short leg sits at 0.80 spot: past it the spread stops paying and the
    # deeper fall goes straight through to the book, so the hedged line turns
    # back down. The outright put keeps paying all the way, which is the whole
    # difference between the two and costs exactly what the table says.
    assert at(spread, -0.30)["hedged_pnl"] < at(spread, -0.20)["hedged_pnl"]
    assert at(put, -0.30)["hedged_pnl"] > at(put, -0.20)["hedged_pnl"]
    assert at(put, -0.30)["hedge_pnl"] > at(spread, -0.30)["hedge_pnl"]


def test_a_linear_hedge_draws_the_straight_line_it_is(shock_set, book):
    table = _table(shock_set, book, book_rows=ROWS)
    short = next(r for r in table["rows"] if r["kind"] == "short_etf")
    points = short["scenario"]["points"]

    # Sized at beta-dollars against a book that is beta-dollars long, so the
    # hedged line is flat: every dollar of market move is cancelled.
    assert all(abs(p["hedged_pnl"]) < 1e-6 for p in points)
    assert short["scenario"]["exposure"] == "portfolio"
    assert short["scenario"]["tail_shock"] < 0


def test_a_name_hedge_is_drawn_against_that_name_not_the_index(shock_set, book):
    """A collar on AAA moves with AAA, so its beta must not be applied twice."""
    spot = 100.0
    chain = pd.DataFrame([
        {"option_type": "put", "expiration": GOOD_EXPIRY, "strike": 90,
         "bid": 3.0, "ask": 3.4, "implied_volatility": 0.3, "volume": 50,
         "open_interest": 200, "last_trade_date": FRESH},
        {"option_type": "call", "expiration": GOOD_EXPIRY, "strike": 110,
         "bid": 2.6, "ask": 3.0, "implied_volatility": 0.28, "volume": 40,
         "open_interest": 150, "last_trade_date": FRESH},
    ])
    collar, _ = candidates.collar_candidate(chain, "AAA", spot, 500, AS_OF, HORIZON)
    table = candidates.cost_table(
        shock_set, book, BOOK_VALUE, [collar], {"AAA": spot}, AS_OF,
        target_reduction=2_000.0, book_rows=ROWS,
    )
    scenario = table["rows"][0]["scenario"]
    assert scenario["exposure"] == "AAA"
    assert scenario["exposure_value"] == 50_000.0
    down = next(p for p in scenario["points"] if p["shock"] == -0.20)
    assert down["exposure_pnl"] == pytest.approx(-10_000.0)   # not 2x that


def test_the_curve_at_zero_is_exactly_the_cost_the_table_charges(shock_set, book):
    """The picture cannot flatter a candidate the table has already priced.

    Nothing happening is the one point where the two must agree exactly: the
    hedge is worth what the spread and the decay took, and no more.
    """
    table = _table(shock_set, book, book_rows=ROWS)
    for row in table["rows"]:
        flat = next(p for p in row["scenario"]["points"] if p["shock"] == 0.0)
        charged = row["cost_bps"] * BOOK_VALUE / 10_000
        assert flat["hedged_pnl"] == pytest.approx(-charged, abs=0.02)
