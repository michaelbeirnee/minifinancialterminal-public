"""Offline tests for hedge pricing, instruments, and quote hygiene.

Step 2 of docs/hedge-construction.md. Pure functions, canned chains, exact
values: the Black–Scholes number is pinned against the textbook case, parity
identities are checked to float precision, and the chain cleaner is fed one
row of each defect it must catch.
"""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from backend.portfolio import pricing
from backend.portfolio.pricing import LinearHedge, OptionLeg, OptionStructure

AS_OF = date(2026, 8, 13)
EXPIRY = date(2027, 2, 13)  # ~184 days out


def _leg(option_type="put", strike=100.0, quantity=1, iv=0.20, bid=4.0, ask=4.4):
    return OptionLeg(
        option_type=option_type, strike=strike, expiration=EXPIRY,
        quantity=quantity, iv=iv, bid=bid, ask=ask,
    )


# --------------------------------------------------------------------------- #
# Pricing math
# --------------------------------------------------------------------------- #
def test_bs_matches_the_textbook_value():
    # S=K=100, r=5%, q=0, vol=20%, T=0.5: call 6.889, put 4.420.
    call = pricing.bs_price(100, 100, 0.5, 0.20, rate=0.05, option_type="call")
    put = pricing.bs_price(100, 100, 0.5, 0.20, rate=0.05, option_type="put")
    assert call == pytest.approx(6.889, abs=0.01)
    assert put == pytest.approx(4.420, abs=0.01)


def test_put_call_parity_with_dividends():
    s, k, t, vol, r, q = 105.0, 95.0, 0.75, 0.3, 0.04, 0.02
    call = pricing.bs_price(s, k, t, vol, r, q, "call")
    put = pricing.bs_price(s, k, t, vol, r, q, "put")
    parity = s * np.exp(-q * t) - k * np.exp(-r * t)
    assert call - put == pytest.approx(parity, abs=1e-10)


def test_limits_collapse_to_intrinsic():
    assert pricing.bs_price(90, 100, 0.0, 0.2, option_type="put") == 10.0
    assert pricing.bs_price(90, 100, -0.1, 0.2, option_type="call") == 0.0
    # vol=0 with time left: the discounted deterministic-forward payoff.
    assert pricing.bs_price(100, 90, 1.0, 0.0, option_type="call") == pytest.approx(10.0)


def test_delta_signs_and_bounds():
    put = pricing.bs_delta(100, 100, 0.5, 0.2, option_type="put")
    call = pricing.bs_delta(100, 100, 0.5, 0.2, option_type="call")
    assert -1.0 < put < 0.0
    assert 0.0 < call < 1.0
    deep_itm_put = pricing.bs_delta(50, 100, 0.25, 0.2, option_type="put")
    assert deep_itm_put == pytest.approx(-1.0, abs=0.01)


def test_black76_parity():
    f, k, t, vol, r = 22.0, 25.0, 0.25, 0.9, 0.04
    call = pricing.black76_price(f, k, t, vol, r, "call")
    put = pricing.black76_price(f, k, t, vol, r, "put")
    assert call - put == pytest.approx(np.exp(-r * t) * (f - k), abs=1e-10)


# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #
def test_structure_value_reaches_intrinsic_at_expiry():
    structure = OptionStructure("protective_put", "AAA", (_leg(strike=100.0),))
    at_expiry = pricing.structure_value(structure, spot=80.0, as_of=EXPIRY)
    assert at_expiry == pytest.approx(20.0 * 100)
    assert pricing.payoff_at_expiry(structure, 80.0) == pytest.approx(20.0 * 100)
    assert pricing.payoff_at_expiry(structure, 120.0) == 0.0


def test_iv_shift_moves_a_long_put_the_right_way():
    structure = OptionStructure("protective_put", "AAA", (_leg(),))
    base = pricing.structure_value(structure, 100.0, AS_OF)
    shocked = pricing.structure_value(structure, 100.0, AS_OF, iv_shift=0.10)
    crushed = pricing.structure_value(structure, 100.0, AS_OF, iv_shift=-0.50)
    assert shocked > base          # vega gain in the down state
    assert crushed >= 0.0          # vol floored, never a negative price


def test_put_spread_is_worth_less_than_its_long_leg():
    long_put = _leg(strike=100.0, quantity=1)
    short_put = _leg(strike=90.0, quantity=-1, bid=1.8, ask=2.1)
    spread = OptionStructure("put_spread", "AAA", (long_put, short_put))
    alone = OptionStructure("protective_put", "AAA", (long_put,))
    assert pricing.structure_value(spread, 100.0, AS_OF) < pricing.structure_value(
        alone, 100.0, AS_OF
    )
    # Payoff capped at the width of the strikes.
    assert pricing.payoff_at_expiry(spread, 50.0) == pytest.approx(10.0 * 100)


def test_entry_cost_uses_the_executable_side():
    collar = OptionStructure(
        "collar", "AAA",
        (_leg("put", 90.0, 1, 0.25, bid=2.0, ask=2.3),
         _leg("call", 110.0, -1, 0.22, bid=2.1, ask=2.4)),
    )
    # Long put at ask (2.3), short call at bid (2.1): 0.2 * 100 to pay.
    assert pricing.entry_cost(collar) == pytest.approx(0.2 * 100, abs=1e-9)
    assert pricing.mid_cost(collar) == pytest.approx(-0.1 * 100, abs=1e-9)

    unquoted = OptionStructure(
        "collar", "AAA",
        (_leg("put", 90.0, 1, 0.25, bid=2.0, ask=2.3),
         _leg("call", 110.0, -1, 0.22, bid=None, ask=2.4)),
    )
    assert pricing.entry_cost(unquoted) is None


def test_linear_hedge_pnl_is_short():
    hedge = LinearHedge("short_etf", "SPY", notional=50_000.0, beta=1.0)
    assert hedge.pnl(-0.10) == pytest.approx(5_000.0)
    assert hedge.pnl(0.10) == pytest.approx(-5_000.0)


# --------------------------------------------------------------------------- #
# Quote hygiene
# --------------------------------------------------------------------------- #
def _chain_frame(rows):
    return pd.DataFrame(rows)


def test_clean_chain_drops_each_defect_once():
    good = {"option_type": "put", "expiration": "2027-02-13", "strike": 100.0,
            "bid": 4.0, "ask": 4.4, "implied_volatility": 0.2,
            "last_trade_date": "2026-08-12", "contract_symbol": "AAA_P100"}
    rows = [
        good,
        {**good, "strike": 105.0, "bid": 4.8, "ask": 5.2},          # good too
        {**good, "strike": 95.0, "bid": 0.0},                       # zero bid
        {**good, "strike": 108.0, "bid": 5.9, "ask": 5.5},          # crossed
        {**good, "strike": 110.0, "last_trade_date": "2026-06-01"}, # stale
        # A 90-strike put quoted above the 100 and 105 puts: the outlier is
        # dropped by the majority, not kept for being the first strike.
        {**good, "strike": 90.0, "bid": 6.0, "ask": 6.4},
    ]
    cleaned, counts = pricing.clean_chain(_chain_frame(rows), AS_OF)
    assert counts == {"zero_bid": 1, "crossed": 1, "stale": 1,
                      "nonmonotonic": 1, "kept": 2}
    assert list(cleaned["strike"]) == [100.0, 105.0]


def test_clean_chain_without_quotes_keeps_nothing():
    frame = _chain_frame([{"option_type": "put", "strike": 100.0}])
    cleaned, counts = pricing.clean_chain(frame, AS_OF)
    assert cleaned.empty
    assert counts["zero_bid"] == 1


def test_leg_from_chain_row_roundtrips():
    cleaned, _counts = pricing.clean_chain(_chain_frame([{
        "option_type": "put", "expiration": "2027-02-13", "strike": 100.0,
        "bid": 4.0, "ask": 4.4, "implied_volatility": 0.2,
        "last_trade_date": "2026-08-12", "contract_symbol": "AAA_P100",
    }]), AS_OF)
    leg = OptionLeg.from_chain_row(cleaned.iloc[0], quantity=2)
    assert leg.strike == 100.0
    assert leg.expiration == date(2027, 2, 13)
    assert leg.quantity == 2
    assert leg.iv == pytest.approx(0.2)
    assert leg.bid == 4.0 and leg.ask == 4.4
