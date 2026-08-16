"""Offline tests for the historical-shock distribution engine.

Step 3 of docs/hedge-construction.md. Canned prices with exactly known
structure: AAA is 2x the benchmark, BBB 0.5x, CCC 1.5x but listed halfway
through the sample (exercising the beta + residual fallback), and the vol
index is built anti-correlated with the benchmark so the sticky-strike IV
shift pays the put in exactly the windows it should.
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from backend.portfolio import pricing, shocks

DATES = pd.bdate_range("2025-06-02", periods=300)
BENCH_RETURNS = 0.0005 + 0.012 * np.sin(0.7 * np.arange(len(DATES)))
AS_OF = date(2026, 8, 13)
HORIZON = 21


def _prices(start_price, factor):
    return pd.Series(start_price * np.cumprod(1.0 + factor * BENCH_RETURNS), index=DATES)


SPY = _prices(500.0, 1.0)
AAA = _prices(100.0, 2.0)
BBB = _prices(50.0, 0.5)
CCC = _prices(80.0, 1.5)
CCC.iloc[:150] = np.nan  # listed mid-sample
VIX = 20.0 - 40.0 * (SPY / SPY.iloc[0] - 1.0)

PANEL = pd.DataFrame({"AAA": AAA, "BBB": BBB, "CCC": CCC})
ROWS = [
    {"symbol": "AAA", "market_value": 50_000.0},
    {"symbol": "BBB", "market_value": 30_000.0},
]


@pytest.fixture(scope="module")
def shock_set():
    return shocks.build_shocks(PANEL, SPY, HORIZON, benchmark="SPY", vol_closes=VIX)


def _put(strike_ratio=0.95, iv=0.20, quantity=1, days_out=120):
    spot = float(SPY.iloc[-1])
    leg = pricing.OptionLeg(
        option_type="put",
        strike=round(strike_ratio * spot),
        expiration=AS_OF + timedelta(days=days_out),
        quantity=quantity,
        iv=iv,
    )
    return pricing.OptionStructure("protective_put", "SPY", (leg,))


# --------------------------------------------------------------------------- #
# Shock construction
# --------------------------------------------------------------------------- #
def test_windows_match_hand_computed_returns(shock_set):
    assert shock_set.n_windows == len(DATES) - HORIZON
    assert shock_set.n_independent == shock_set.n_windows // HORIZON
    i = 40
    expected_bench = SPY.iloc[i + HORIZON] / SPY.iloc[i] - 1.0
    expected_aaa = AAA.iloc[i + HORIZON] / AAA.iloc[i] - 1.0
    assert shock_set.benchmark_return.iloc[i] == pytest.approx(expected_bench, abs=1e-12)
    assert shock_set.holding_returns["AAA"].iloc[i] == pytest.approx(expected_aaa, abs=1e-12)


def test_betas_recovered_exactly(shock_set):
    assert shock_set.betas["AAA"] == pytest.approx(2.0, abs=1e-9)
    assert shock_set.betas["BBB"] == pytest.approx(0.5, abs=1e-9)
    assert shock_set.betas["CCC"] == pytest.approx(1.5, abs=1e-6)


def test_short_history_holding_is_filled_and_flagged(shock_set):
    assert shock_set.fallback_symbols == ["CCC"]
    assert not shock_set.holding_returns["CCC"].isna().any()


def test_iv_shift_tracks_the_vol_index(shock_set):
    i = 25
    expected = (VIX.iloc[i + HORIZON] - VIX.iloc[i]) / 100.0
    assert shock_set.iv_shift.iloc[i] == pytest.approx(expected, abs=1e-12)
    # Vol index built anti-correlated with the benchmark: down markets, vol up.
    corr = np.corrcoef(shock_set.iv_shift, shock_set.benchmark_return)[0, 1]
    assert corr < -0.9


def test_frozen_iv_is_flagged_loudly():
    frozen = shocks.build_shocks(PANEL, SPY, HORIZON, vol_closes=None)
    assert (frozen.iv_shift == 0.0).all()
    assert any("UNDERSTATED" in note for note in frozen.notes)


def test_build_is_deterministic():
    first = shocks.build_shocks(PANEL, SPY, HORIZON, vol_closes=VIX)
    second = shocks.build_shocks(PANEL, SPY, HORIZON, vol_closes=VIX)
    pd.testing.assert_frame_equal(first.holding_returns, second.holding_returns)


# --------------------------------------------------------------------------- #
# Distributions
# --------------------------------------------------------------------------- #
def test_book_pnl_is_the_dot_product(shock_set):
    pnl = shocks.book_pnl(shock_set, ROWS)
    expected = (
        50_000.0 * shock_set.holding_returns["AAA"].to_numpy()
        + 30_000.0 * shock_set.holding_returns["BBB"].to_numpy()
    )
    assert np.allclose(pnl, expected)


def test_missing_symbol_is_noted_not_silent(shock_set):
    pnl = shocks.book_pnl(shock_set, [{"symbol": "ZZZ", "market_value": 1000.0}])
    assert np.allclose(pnl, 0.0)
    assert any("ZZZ" in note for note in shock_set.notes)


def test_put_pays_in_the_down_windows(shock_set):
    unit = shocks.hedge_unit_pnl(shock_set, _put(), float(SPY.iloc[-1]), AS_OF)
    corr = np.corrcoef(unit, shock_set.benchmark_return)[0, 1]
    assert corr < -0.8
    worst = int(np.argmin(shock_set.benchmark_return.to_numpy()))
    assert unit[worst] > 0


def test_unknown_underlying_raises(shock_set):
    structure = pricing.OptionStructure("protective_put", "QQQ", _put().legs)
    with pytest.raises(ValueError):
        shocks.hedge_unit_pnl(shock_set, structure, 400.0, AS_OF)


def test_cvar_curve_shows_increasing_protection(shock_set):
    book = shocks.book_pnl(shock_set, ROWS)
    unit = shocks.hedge_unit_pnl(shock_set, _put(), float(SPY.iloc[-1]), AS_OF)
    curve = shocks.cvar_curve(book, unit, [0, 1, 2])
    assert curve[0]["cvar_reduction"] == 0.0
    assert curve[1]["cvar_reduction"] > 0
    assert curve[2]["cvar_reduction"] > curve[1]["cvar_reduction"]
    assert curve[1]["cvar_hedged"] > curve[1]["cvar_unhedged"]  # less negative


def test_protection_ci_is_seeded_and_contains_the_point(shock_set):
    book = shocks.book_pnl(shock_set, ROWS)
    unit = shocks.hedge_unit_pnl(shock_set, _put(), float(SPY.iloc[-1]), AS_OF)
    first = shocks.protection_ci(book, unit, quantity=1)
    second = shocks.protection_ci(book, unit, quantity=1)
    assert first == second
    low, high = first["cvar_reduction_ci95"]
    assert low <= first["cvar_reduction"] <= high


# --------------------------------------------------------------------------- #
# Display grid
# --------------------------------------------------------------------------- #
def test_scenario_grid_shows_protection_without_ranking(shock_set):
    spot = float(SPY.iloc[-1])
    grid = shocks.scenario_grid(shock_set, ROWS, _put(), 2, spot, AS_OF)
    assert len(grid) == len(shocks.DEFAULT_INDEX_SHOCKS) * len(shocks.DEFAULT_IV_SHIFTS)

    crash = next(r for r in grid if r["index_shock"] == -0.30 and r["iv_shift"] == 0.10)
    rally = next(r for r in grid if r["index_shock"] == 0.20 and r["iv_shift"] == 0.0)
    assert crash["hedged_pnl"] > crash["book_pnl"]      # the put softens the crash
    assert rally["hedge_pnl"] < 0                        # and decays in the rally
    # Book side is beta x shock: AAA at 2x, BBB at 0.5x.
    expected_book = (50_000 * 2.0 + 30_000 * 0.5) * -0.30
    assert crash["book_pnl"] == pytest.approx(expected_book, rel=1e-6)


# --------------------------------------------------------------------------- #
# Display curve
# --------------------------------------------------------------------------- #
def _curve(shock_set, quantity=2, cost=0.0):
    """The plotted curve for ``quantity`` puts, net of ``cost`` to put them on."""
    spot = float(SPY.iloc[-1])
    structure = _put()
    horizon_date = shocks.horizon_end(AS_OF, HORIZON)
    base = pricing.structure_value(structure, spot, AS_OF)

    def hedge_pnl(shock, iv_shift):
        shocked = pricing.structure_value(
            structure, spot * (1.0 + shock), horizon_date, iv_shift
        )
        return quantity * (shocked - base) - cost

    return shocks.scenario_curve(shock_set, ROWS, hedge_pnl, shock_set.betas)


def test_scenario_curve_draws_the_protection_the_grid_prices(shock_set):
    curve = _curve(shock_set)
    assert [p["shock"] for p in curve] == list(shocks.CURVE_SHOCKS)

    crash = next(p for p in curve if p["shock"] == -0.30)
    flat = next(p for p in curve if p["shock"] == 0.0)
    rally = next(p for p in curve if p["shock"] == 0.20)

    expected_book = (50_000 * 2.0 + 30_000 * 0.5) * -0.30
    assert crash["exposure_pnl"] == pytest.approx(expected_book, rel=1e-6)
    assert crash["hedged_pnl"] > crash["exposure_pnl"]   # the put softens the fall
    assert flat["hedge_pnl"] < 0                          # nothing happens, decay bites
    assert rally["hedged_pnl"] < rally["exposure_pnl"]    # the premium is gone


def test_scenario_curve_is_drawn_net_of_what_the_hedge_cost(shock_set):
    free = _curve(shock_set, cost=0.0)
    paid = _curve(shock_set, cost=500.0)
    assert all(
        b["hedged_pnl"] == pytest.approx(a["hedged_pnl"] - 500.0)
        for a, b in zip(free, paid)
    )


def test_scenario_curve_pairs_vol_with_the_move_it_has_historically(shock_set):
    """The frozen line understates a put — the second line says by how much."""
    slope, _intercept = shocks.iv_response(shock_set)
    assert slope < 0                       # vol rises as the market falls

    crash = next(p for p in _curve(shock_set) if p["shock"] == -0.30)
    assert crash["iv_shift"] > 0
    assert crash["hedge_pnl_iv"] > crash["hedge_pnl"]


def test_frozen_iv_is_drawn_as_absent_not_as_unchanged():
    frozen = shocks.build_shocks(PANEL, SPY, HORIZON, benchmark="SPY", vol_closes=None)
    assert shocks.iv_response(frozen) is None
    assert all("hedge_pnl_iv" not in point for point in _curve(frozen))
