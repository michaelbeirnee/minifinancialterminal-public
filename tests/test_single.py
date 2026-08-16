"""Offline tests for the single-name hedge simulator.

Step 5b of docs/hedge-construction.md. Reuses the canned market from
test_shocks (AAA moves 2x the benchmark) so the position arithmetic, the
dollars-to-shares-to-contracts rounding, and the clicked-contract path are
checked against known answers rather than live quotes.

The invariant these guard is the one that separates this entry point from the
portfolio one: a single name is shocked by its *own* history, and a clicked
call is an overwrite that must never be dressed up as protection.
"""
from datetime import timedelta

import pandas as pd
import pytest

from backend.portfolio import candidates, pricing, shocks, single
from tests.test_shocks import AAA, AS_OF, HORIZON, VIX

SPOT = float(AAA.iloc[-1])
FRESH = (AS_OF - timedelta(days=1)).isoformat()
GOOD_EXPIRY = (AS_OF + timedelta(days=120)).isoformat()


def _row(option_type, moneyness, mid, **overrides):
    row = {
        "option_type": option_type, "expiration": GOOD_EXPIRY,
        "strike": round(moneyness * SPOT), "bid": mid - 0.2, "ask": mid + 0.2,
        "implied_volatility": 0.30, "volume": 200, "open_interest": 800,
        "last_trade_date": FRESH,
        "contract_symbol": "AAA-{}-{}".format(option_type[0].upper(), moneyness),
    }
    row.update(overrides)
    return row


def _chain():
    """Puts rising in strike, calls falling — a clean monotone surface."""
    return pd.DataFrame(
        [_row("put", m, v) for m, v in
         ((0.80, 2.5), (0.85, 4.0), (0.90, 6.5), (0.95, 10.5), (1.00, 16.0))]
        + [_row("call", m, v) for m, v in
           ((1.00, 15.0), (1.05, 11.0), (1.10, 7.5), (1.20, 3.0))]
    )


@pytest.fixture(scope="module")
def position():
    return single.position_from_notional("AAA", AAA, 100 * SPOT)


@pytest.fixture(scope="module")
def shocked(position):
    """The name is its own shock driver — the whole point of this path."""
    shock_set = shocks.build_shocks(
        position.panel, position.closes, HORIZON, benchmark="AAA", vol_closes=VIX
    )
    return shock_set, shocks.book_pnl(shock_set, position.rows)


# --------------------------------------------------------------------------- #
# Sizing: dollars -> shares -> contracts, with both roundings reported
# --------------------------------------------------------------------------- #
def test_notional_floors_to_whole_shares():
    # Deliberately 2.5 shares' worth: the half share must be dropped, not
    # rounded up into a position the user did not ask for.
    position = single.position_from_notional("AAA", AAA, 2.5 * SPOT)
    assert position.shares == 2
    assert position.market_value == pytest.approx(2 * SPOT)
    assert position.sizing()["uninvested_cash"] == pytest.approx(0.5 * SPOT, abs=0.01)


def test_position_under_one_contract_says_so():
    position = single.position_from_notional("AAA", AAA, 50 * SPOT)
    sizing = position.sizing()
    assert position.contracts_covered == 0
    assert "under the 100 a contract covers" in sizing["note"]
    assert "de-risk by selling" in sizing["note"]


def test_notional_below_one_share_is_refused():
    with pytest.raises(ValueError, match="does not buy one share"):
        single.position_from_notional("AAA", AAA, SPOT / 2)


def test_panel_and_rows_are_what_the_shock_engine_reads(position):
    assert list(position.panel.columns) == ["AAA"]
    assert position.rows == [{"symbol": "AAA", "market_value": position.market_value}]


# --------------------------------------------------------------------------- #
# The name drives its own shocks
# --------------------------------------------------------------------------- #
def test_the_name_is_its_own_benchmark(shocked):
    shock_set, _ = shocked
    assert shock_set.benchmark == "AAA"
    # Beta against itself is 1.0 by construction; anything else means the
    # driver series and the holding series have come apart.
    assert shock_set.betas["AAA"] == pytest.approx(1.0, abs=1e-9)


def test_book_pnl_scales_with_the_position(position, shocked):
    shock_set, book = shocked
    doubled = shocks.book_pnl(
        shock_set, [{"symbol": "AAA", "market_value": 2 * position.market_value}]
    )
    assert doubled == pytest.approx(2 * book)


# --------------------------------------------------------------------------- #
# Candidate construction on the name's own chain
# --------------------------------------------------------------------------- #
def test_name_candidates_respect_the_instrument_filter(position):
    built, _ = single.name_candidates(
        _chain(), position, AS_OF, HORIZON, ("protective_put",)
    )
    assert [c.structure.kind for c in built] == ["protective_put"]
    assert all(c.structure.underlying == "AAA" for c in built)


def test_collar_is_sized_by_shares_not_solved(position):
    built, _ = single.name_candidates(_chain(), position, AS_OF, HORIZON, ("collar",))
    collar = next(c for c in built if c.structure.kind == "collar")
    assert collar.fixed_quantity == position.contracts_covered == 1


# --------------------------------------------------------------------------- #
# The clicked contract
# --------------------------------------------------------------------------- #
def test_clicked_put_becomes_a_long_protective_leg(position):
    candidate, skipped = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, round(0.90 * SPOT), "put"
    )
    assert skipped == []
    assert candidate.structure.kind == "protective_put"
    (leg,) = candidate.structure.legs
    assert leg.quantity == 1 and leg.option_type == "put"
    assert leg.strike == round(0.90 * SPOT)
    # Unpinned count means the solver still sizes it against the target.
    assert candidate.fixed_quantity is None


def test_clicked_call_is_sold_and_named_an_overwrite(position):
    candidate, skipped = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, round(1.10 * SPOT), "call"
    )
    assert skipped == []
    assert candidate.structure.kind == "covered_call"
    assert candidate.structure.kind in single.OVERWRITE_KINDS
    (leg,) = candidate.structure.legs
    assert leg.quantity == -1, "a call against a long position is written, not bought"


def test_written_calls_never_exceed_the_shares_held():
    """Asking for 5 contracts against 100 shares must not go naked."""
    position = single.position_from_notional("AAA", AAA, 100 * SPOT)
    candidate, _ = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, round(1.10 * SPOT), "call", contracts=5
    )
    assert candidate.fixed_quantity == 1 == position.contracts_covered


def test_call_on_a_sub_contract_position_is_refused():
    position = single.position_from_notional("AAA", AAA, 50 * SPOT)
    candidate, skipped = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, round(1.10 * SPOT), "call"
    )
    assert candidate is None
    assert "naked short" in skipped[0]["reason"]


def test_pinned_put_count_is_honoured(position):
    candidate, _ = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, round(0.90 * SPOT), "put", contracts=3
    )
    assert candidate.fixed_quantity == 3


def test_unquotable_contract_says_why_rather_than_guessing(position):
    candidate, skipped = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, 999_999, "put"
    )
    assert candidate is None
    assert "no quote that survives hygiene" in skipped[0]["reason"]


def test_a_zero_bid_contract_is_dropped_by_hygiene(position):
    chain = pd.concat(
        [_chain(), pd.DataFrame([_row("put", 0.60, 1.0, bid=0.0)])], ignore_index=True
    )
    candidate, skipped = single.contract_candidate(
        chain, position, AS_OF, GOOD_EXPIRY, round(0.60 * SPOT), "put"
    )
    assert candidate is None, "a zero bid is not an executable quote"
    assert skipped


def test_bad_option_type_is_rejected(position):
    candidate, skipped = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, SPOT, "future"
    )
    assert candidate is None
    assert "call or put" in skipped[0]["reason"]


# --------------------------------------------------------------------------- #
# End to end through the shared cost table
# --------------------------------------------------------------------------- #
def test_protective_put_reaches_a_target_and_costs_money(position, shocked):
    shock_set, book = shocked
    built, _ = single.name_candidates(
        _chain(), position, AS_OF, HORIZON, ("protective_put",)
    )
    unhedged = shocks.cvar(book, 0.05)
    table = candidates.cost_table(
        shock_set, book, position.market_value, built, {"AAA": SPOT}, AS_OF,
        target_reduction=abs(unhedged) * 0.5,
    )
    (row,) = table["rows"]
    assert row["kind"] == "protective_put"
    assert row["protection_bps"] > 0, "a long put must cut the left tail"
    assert row["cost_bps"] > 0, "protection is paid for, never free"


def _row_for(candidate, position, shocked):
    shock_set, book = shocked
    table = candidates.cost_table(
        shock_set, book, position.market_value, [candidate], {"AAA": SPOT}, AS_OF,
        target_reduction=abs(shocks.cvar(book, 0.05)) * 0.5,
    )
    (row,) = table["rows"]
    return row


def test_an_overwrite_is_paid_for_in_upside_not_cash(position, shocked):
    """A covered call takes in premium and pays for it if the stock rallies."""
    call, _ = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, round(1.05 * SPOT), "call"
    )
    row = _row_for(call, position, shocked)
    assert row["kind"] == "covered_call" and row["kind"] in single.OVERWRITE_KINDS
    assert row["cost_bps"] < 0, "writing a call takes in premium"
    assert all(v < 0 for v in row["upside_loss"].values()), "the cap is a real cost"


def test_the_cap_grows_with_the_rally_but_a_put_premium_does_not(position, shocked):
    """The economic difference between an overwrite and protection.

    A bought put's worst upside case is the premium — it cannot cost more than
    it cost. A written call's give-up is unbounded in the rally, which is why
    the two must never be ranked as if they were the same kind of thing.
    """
    call, _ = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, round(1.05 * SPOT), "call"
    )
    put, _ = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, round(0.90 * SPOT), "put"
    )
    call_up = _row_for(call, position, shocked)["upside_loss"]
    put_up = _row_for(put, position, shocked)["upside_loss"]

    assert call_up["+20%"] < call_up["+10%"], "a deeper rally costs the writer more"
    assert put_up["+20%"] == pytest.approx(put_up["+10%"], rel=0.25), (
        "a long put's upside give-up is the premium, near-flat across rallies"
    )
    assert call_up["+20%"] < put_up["+20%"], "the cap bites harder than the premium"


def test_the_curve_shows_an_overwrite_capping_the_upside(position, shocked):
    """The picture the warning beside it is trying to make: premium is not cover.

    A written call's cushion stops growing the moment the premium is used up,
    while the fall keeps going — and on the other side the give-up never stops
    growing at all. A cost table of averages cannot show either shape.
    """
    shock_set, book = shocked
    call, _ = single.contract_candidate(
        _chain(), position, AS_OF, GOOD_EXPIRY, round(1.05 * SPOT), "call"
    )
    table = candidates.cost_table(
        shock_set, book, position.market_value, [call], {"AAA": SPOT}, AS_OF,
        target_reduction=abs(shocks.cvar(book, 0.05)) * 0.5,
        book_rows=position.rows,
    )
    scenario = table["rows"][0]["scenario"]
    assert scenario["exposure"] == "AAA"          # the name, not an index proxy
    assert scenario["tail_shock"] < 0

    at = {point["shock"]: point for point in scenario["points"]}
    cushion = {s: at[s]["hedged_pnl"] - at[s]["exposure_pnl"] for s in (-0.30, -0.20, 0.10, 0.20)}

    # Down: the cushion has stopped growing well before the worst of it, and
    # never covered much of the fall to begin with.
    assert cushion[-0.30] == pytest.approx(cushion[-0.20], rel=0.10)
    assert cushion[-0.30] < abs(at[-0.30]["exposure_pnl"]) * 0.25
    # Up: the give-up keeps growing with the rally, which is the real price.
    assert cushion[0.20] < cushion[0.10] < 0
