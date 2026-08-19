"""Black-Scholes pricing, greeks and implied vol — math first, commands after.

The math tests are pure and offline, checked against hand-verified reference
values (Hull's textbook example among them) and against the model's own
identities: put-call parity, the IV round-trip, delta bounds, gamma/vega
equality across the pair. The command tests mock the market inputs so no
network is touched.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.valuation import blackscholes as bs


# --------------------------------------------------------------------------- #
# Reference values
# --------------------------------------------------------------------------- #
def test_hull_reference_price():
    """Hull, Options Futures & Other Derivatives: S=42, K=40, r=10%, sigma=20%, T=0.5."""
    call = float(bs.bs_price(42, 40, 0.5, 0.2, r=0.10, option_type="call")[0])
    put = float(bs.bs_price(42, 40, 0.5, 0.2, r=0.10, option_type="put")[0])
    assert call == pytest.approx(4.7594, abs=1e-4)
    assert put == pytest.approx(0.8086, abs=1e-4)


def test_atm_zero_rate_symmetry():
    """With r=q=0, an ATM call and put are worth the same."""
    c = float(bs.bs_price(100, 100, 1.0, 0.25, option_type="call")[0])
    p = float(bs.bs_price(100, 100, 1.0, 0.25, option_type="put")[0])
    assert c == pytest.approx(p, rel=1e-12)
    # and ~ 0.3989 * sigma * sqrt(T) * S, the classic ATM approximation
    assert c == pytest.approx(0.3989 * 0.25 * 100, rel=1e-2)


def test_put_call_parity_holds_with_dividends():
    s, k, t, sig, r, q = 315.0, 320.0, 45 / 365, 0.27, 0.0388, 0.0035
    c = float(bs.bs_price(s, k, t, sig, r, q, "call")[0])
    p = float(bs.bs_price(s, k, t, sig, r, q, "put")[0])
    assert c - p == pytest.approx(s * np.exp(-q * t) - k * np.exp(-r * t), abs=1e-10)


def test_expired_option_is_intrinsic():
    assert float(bs.bs_price(110, 100, 0.0, 0.3, option_type="call")[0]) == 10.0
    assert float(bs.bs_price(110, 100, 0.0, 0.3, option_type="put")[0]) == 0.0


# --------------------------------------------------------------------------- #
# Greeks
# --------------------------------------------------------------------------- #
def test_greeks_reference_and_identities():
    g_c = bs.bs_greeks(42, 40, 0.5, 0.2, r=0.10, option_type="call")
    g_p = bs.bs_greeks(42, 40, 0.5, 0.2, r=0.10, option_type="put")
    # Hull's d1 = 0.7693: N(d1) = 0.7791
    assert float(g_c["delta"][0]) == pytest.approx(0.7791, abs=1e-4)
    assert float(g_p["delta"][0]) == pytest.approx(0.7791 - 1, abs=1e-4)  # q=0 parity
    # gamma and vega are the same for the call and the put
    assert float(g_c["gamma"][0]) == pytest.approx(float(g_p["gamma"][0]), rel=1e-12)
    assert float(g_c["vega"][0]) == pytest.approx(float(g_p["vega"][0]), rel=1e-12)
    assert float(g_c["gamma"][0]) > 0
    # theta per day, negative for both here; rho signs by side
    assert float(g_c["theta"][0]) < 0 and float(g_p["theta"][0]) < 0
    assert float(g_c["rho"][0]) > 0 > float(g_p["rho"][0])


def test_greeks_match_finite_differences():
    s, k, t, sig, r, q = 100.0, 105.0, 0.25, 0.3, 0.04, 0.01
    g = bs.bs_greeks(s, k, t, sig, r, q, "put")
    eps = 1e-4
    price = lambda **kw: float(bs.bs_price(  # noqa: E731
        kw.get("s", s), k, kw.get("t", t), kw.get("sig", sig),
        kw.get("r", r), q, "put")[0])
    assert float(g["delta"][0]) == pytest.approx(
        (price(s=s + eps) - price(s=s - eps)) / (2 * eps), abs=1e-5)
    assert float(g["gamma"][0]) == pytest.approx(
        (price(s=s + eps) - 2 * price() + price(s=s - eps)) / eps**2, abs=1e-4)
    assert float(g["vega"][0]) == pytest.approx(
        (price(sig=sig + eps) - price(sig=sig - eps)) / (2 * eps) / 100, abs=1e-5)
    assert float(g["theta"][0]) == pytest.approx(
        -(price(t=t + eps) - price(t=t - eps)) / (2 * eps) / 365, abs=1e-5)
    assert float(g["rho"][0]) == pytest.approx(
        (price(r=r + eps) - price(r=r - eps)) / (2 * eps) / 100, abs=1e-5)


def test_delta_bounds_across_moneyness():
    strikes = np.array([50, 80, 100, 120, 200], dtype=float)
    g = bs.bs_greeks(100, strikes, 0.5, 0.25, r=0.03, option_type="call")
    deltas = g["delta"]
    assert np.all(np.diff(deltas) < 0)          # deeper ITM -> higher delta
    assert np.all((deltas > 0) & (deltas < 1))


def test_expired_greeks_are_flat():
    g = bs.bs_greeks(110, 100, 0.0, 0.3, option_type="call")
    assert float(g["delta"][0]) == 1.0
    for name in ("gamma", "theta", "vega", "rho"):
        assert float(g[name][0]) == 0.0


# --------------------------------------------------------------------------- #
# Implied vol
# --------------------------------------------------------------------------- #
def test_iv_round_trip_vectorised():
    sigmas = np.array([0.08, 0.2, 0.45, 1.5])
    strikes = np.array([90.0, 100.0, 110.0, 130.0])
    types = np.array(["call", "put", "call", "put"])
    prices = bs.bs_price(100, strikes, 0.3, sigmas, r=0.04, q=0.01, option_type=types)
    back = bs.implied_vol(prices, 100, strikes, 0.3, r=0.04, q=0.01, option_type=types)
    assert np.allclose(back, sigmas, atol=1e-5)


def test_iv_refuses_arbitrage_violations():
    # A call quoted below discounted intrinsic has no BS vol; neither does a free option.
    below = bs.implied_vol(9.0, 110, 100, 0.5, r=0.05, option_type="call")
    assert np.isnan(below[0])
    assert np.isnan(bs.implied_vol(0.0, 100, 100, 0.5, option_type="call")[0])
    assert np.isnan(bs.implied_vol(150.0, 100, 100, 0.5, option_type="call")[0])


def test_option_type_validation():
    with pytest.raises(ValueError):
        bs.bs_price(100, 100, 1, 0.2, option_type="straddle")


# --------------------------------------------------------------------------- #
# The commands, with market inputs mocked
# --------------------------------------------------------------------------- #
@pytest.fixture()
def market(monkeypatch):
    """Fixed spot/yield/curve and a tiny two-sided chain, no network."""
    import pandas as pd

    from backend.extensions import derivatives as d

    monkeypatch.setattr(d.yahoo, "quote",
                        lambda s: {"last_price": 100.0, "dividend_yield": 2.0})
    from backend.providers import treasury

    curve = pd.DataFrame({"maturity_years": [0.083, 0.25, 1.0, 2.0],
                          "rate": [4.0, 4.0, 4.0, 4.0]})
    monkeypatch.setattr(treasury, "yield_curve", lambda *a, **k: curve)

    expiry = str((pd.Timestamp.today().normalize() + pd.Timedelta(days=30)).date())
    sig = 0.25
    t = 30 / 365
    rows = []
    for k_strike in (90.0, 100.0, 110.0):
        for side in ("call", "put"):
            fair = float(bs.bs_price(100.0, k_strike, t, sig, r=0.04, q=0.02, option_type=side)[0])
            rows.append({"contract_symbol": f"X{side[0]}{int(k_strike)}", "strike": k_strike,
                         "option_type": side, "expiration": expiry,
                         "underlying_symbol": "XYZ", "last_price": round(fair, 4),
                         "bid": round(fair - 0.02, 4), "ask": round(fair + 0.02, 4),
                         "volume": 10, "open_interest": 100, "implied_volatility": 0.30})
    chain = pd.DataFrame(rows)
    monkeypatch.setattr(d.yahoo, "option_chain", lambda s, e=None: chain.copy())
    return {"sigma": sig, "expiry": expiry}


def test_greeks_command_provider_iv(market):
    from backend.extensions.derivatives import option_greeks

    res = option_greeks(symbol="XYZ")
    df = res.data
    assert len(df) == 6
    assert {"iv", "bs_price", "delta", "gamma", "theta", "vega", "rho"} <= set(df.columns)
    assert (df["iv"] == 0.30).all()            # provider IV used as-is
    assert res.extra["spot"] == 100.0
    assert res.extra["dividend_yield"] == pytest.approx(0.02)   # percent -> fraction
    assert res.extra["risk_free_rate"] == pytest.approx(0.04)   # read off the curve
    calls = df[df["option_type"] == "call"].sort_values("strike")
    assert np.all(np.diff(calls["delta"]) < 0)


def test_greeks_command_solved_iv_recovers_the_vol(market):
    from backend.extensions.derivatives import option_greeks

    res = option_greeks(symbol="XYZ", iv_source="solved", option_type="call")
    df = res.data
    # The chain was priced at sigma=0.25; solving from the mid should find it.
    assert np.allclose(df["iv"], market["sigma"], atol=0.01)
    assert any("solved from the quote mid" in w for w in res.warnings)


def test_greeks_command_rejects_bad_iv_source(market):
    from backend.extensions.derivatives import option_greeks

    with pytest.raises(ValueError):
        option_greeks(symbol="XYZ", iv_source="astrology")


def test_pricer_prices_both_sides_and_checks_parity(market):
    from backend.extensions.derivatives import option_pricer

    res = option_pricer(k=105, s=100, dte=30, sigma=0.25, r=0.04, q=0.0)
    assert [row["option_type"] for row in res.data] == ["call", "put"]
    assert res.extra["put_call_parity_gap"] == pytest.approx(0.0, abs=1e-7)
    assert res.extra["units"]["theta"] == "per calendar day"


def test_pricer_solves_iv_from_price(market):
    from backend.extensions.derivatives import option_pricer

    fair = float(bs.bs_price(100, 105, 30 / 365, 0.25, r=0.04, option_type="call")[0])
    res = option_pricer(k=105, s=100, dte=30, price=fair, option_type="call", r=0.04, q=0.0)
    assert res.extra["sigma"] == pytest.approx(0.25, abs=1e-4)
    assert res.data[0]["price"] == pytest.approx(fair, abs=1e-4)


def test_pricer_fetches_spot_from_symbol(market):
    from backend.extensions.derivatives import option_pricer

    res = option_pricer(k=105, symbol="XYZ", dte=30, sigma=0.25)
    assert res.extra["spot"] == 100.0
    assert res.extra["dividend_yield"] == pytest.approx(0.02)


def test_pricer_input_validation(market):
    from backend.extensions.derivatives import option_pricer

    with pytest.raises(ValueError):
        option_pricer(k=105, s=100, dte=30)                      # neither sigma nor price
    with pytest.raises(ValueError):
        option_pricer(k=105, s=100, dte=30, sigma=0.2, price=5)  # both
    with pytest.raises(ValueError):
        option_pricer(k=105, s=100, dte=30, price=5)             # IV solve needs a side
    with pytest.raises(ValueError):
        option_pricer(k=105, dte=30, sigma=0.2)                  # no spot, no symbol


def test_pricer_reports_unreachable_price(market):
    from backend.core.errors import EmptyDataError
    from backend.extensions.derivatives import option_pricer

    with pytest.raises(EmptyDataError):
        option_pricer(k=100, s=110, dte=180, price=5.0, option_type="call", r=0.05, q=0.0)


def test_pricer_survives_a_dead_curve(monkeypatch):
    from backend.extensions import derivatives as d
    from backend.providers import treasury

    def boom(*a, **k):
        raise RuntimeError("treasury.gov is down")

    monkeypatch.setattr(treasury, "yield_curve", boom)
    res = d.option_pricer(k=100, s=100, dte=30, sigma=0.2)
    assert res.extra["risk_free_rate"] == d.DEFAULT_RATE
    assert any("flat" in w for w in res.warnings)
