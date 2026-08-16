"""Indicator and estimator maths — deterministic, no network required."""
import numpy as np
import pandas as pd
import pytest

from backend.extensions import indicators as ta


@pytest.fixture(scope="module")
def bars():
    """A reproducible OHLCV frame with a mild uptrend plus noise."""
    rng = np.random.default_rng(7)
    n = 300
    index = pd.date_range("2022-01-03", periods=n, freq="B")
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0006, 0.011, n))), index=index)
    high = close * (1 + rng.uniform(0.001, 0.012, n))
    low = close * (1 - rng.uniform(0.001, 0.012, n))
    volume = pd.Series(rng.integers(1_000_000, 5_000_000, n).astype(float), index=index)
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "high": high,
                         "low": low, "close": close, "volume": volume})


def test_sma_matches_manual_mean(bars):
    out = ta.sma(bars["close"], 10)
    assert out.isna().sum() == 9
    assert out.iloc[20] == pytest.approx(bars["close"].iloc[11:21].mean())


def test_ema_is_recursive_with_alpha_two_over_n_plus_one(bars):
    length = 12
    alpha = 2 / (length + 1)
    out = ta.ema(bars["close"], length)
    expected = out.iloc[5] * (1 - alpha) + bars["close"].iloc[6] * alpha
    assert out.iloc[6] == pytest.approx(expected)


def test_rsi_is_bounded_and_saturates_on_a_monotonic_series():
    rising = pd.Series(np.arange(1, 80, dtype=float))
    out = ta.rsi(rising, 14).dropna()
    assert ((out >= 0) & (out <= 100)).all()
    assert out.iloc[-1] == pytest.approx(100.0)


def test_macd_histogram_is_line_minus_signal(bars):
    out = ta.macd(bars["close"])
    diff = (out["macd"] - out["macd_signal"]).dropna()
    assert np.allclose(diff, out["macd_histogram"].dropna())


def test_bollinger_bands_are_ordered_and_percent_b_tracks_price(bars):
    out = ta.bollinger(bars["close"], 20, 2.0).dropna()
    assert (out["bb_upper"] > out["bb_middle"]).all()
    assert (out["bb_middle"] > out["bb_lower"]).all()
    assert ((out["bb_percent"] > 1) == (bars["close"].loc[out.index] > out["bb_upper"])).all()


def test_atr_is_positive_and_bounded_by_the_widest_range(bars):
    out = ta.atr(bars["high"], bars["low"], bars["close"], 14).dropna()
    assert (out > 0).all()
    assert out.max() <= ta.true_range(bars["high"], bars["low"], bars["close"]).max()


def test_stochastic_stays_within_zero_and_one_hundred(bars):
    out = ta.stochastic(bars["high"], bars["low"], bars["close"]).dropna()
    assert ((out["stoch_k"] >= -1e-9) & (out["stoch_k"] <= 100 + 1e-9)).all()


def test_adx_components_are_percentages(bars):
    out = ta.adx(bars["high"], bars["low"], bars["close"], 14).dropna()
    assert ((out["adx"] >= 0) & (out["adx"] <= 100)).all()
    assert ((out["plus_di"] >= 0) & (out["plus_di"] <= 100)).all()


def test_aroon_up_pins_at_one_hundred_when_every_bar_is_a_new_high():
    n = 60
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    rising = pd.Series(np.arange(1.0, n + 1), index=idx)
    out = ta.aroon(rising, rising - 0.5, 25).dropna()
    assert out["aroon_up"].iloc[-1] == pytest.approx(100.0)
    assert out["aroon_down"].iloc[-1] == pytest.approx(0.0)


def test_obv_accumulates_all_volume_on_a_rising_series():
    close = pd.Series([1.0, 2, 3, 4, 5])
    volume = pd.Series([10.0, 10, 10, 10, 10])
    assert ta.obv(close, volume).iloc[-1] == pytest.approx(40.0)


def test_donchian_channel_brackets_price(bars):
    out = ta.donchian(bars["high"], bars["low"], 20).dropna()
    close = bars["close"].loc[out.index]
    assert (out["dc_upper"] >= close).all()
    assert (out["dc_lower"] <= close).all()


def test_vwap_sits_inside_the_price_range(bars):
    out = ta.vwap(bars["high"], bars["low"], bars["close"], bars["volume"]).dropna()
    assert out.min() >= bars["low"].min()
    assert out.max() <= bars["high"].max()


def test_money_flow_index_is_bounded(bars):
    out = ta.money_flow_index(bars["high"], bars["low"], bars["close"], bars["volume"]).dropna()
    assert ((out >= 0) & (out <= 100)).all()


def test_hurst_exponent_separates_mean_reversion_from_a_random_walk():
    rng = np.random.default_rng(3)
    walk = pd.Series(100 + np.cumsum(rng.normal(0, 1, 3000)))

    # Ornstein-Uhlenbeck: pulled back towards 100, so H should sit below 0.5.
    level, values = 100.0, []
    for shock in rng.normal(0, 1, 3000):
        level += 0.25 * (100 - level) + shock
        values.append(level)
    reverting = pd.Series(values)

    assert ta.hurst_exponent(walk) == pytest.approx(0.5, abs=0.12)
    assert ta.hurst_exponent(reverting) < ta.hurst_exponent(walk)


def test_clenow_momentum_is_positive_for_a_clean_uptrend():
    steady = pd.Series(100 * np.exp(np.linspace(0, 0.4, 120)))
    out = ta.clenow_momentum(steady, 90)
    assert out["annualised_slope"] > 0
    assert out["r_squared"] == pytest.approx(1.0, abs=1e-6)


def test_fibonacci_levels_span_the_range(bars):
    out = ta.fibonacci_levels(bars["high"], bars["low"])
    assert len(out) == 9
    assert out["retracement"].max() == pytest.approx(bars["high"].max())


def test_pivot_points_are_ordered(bars):
    out = ta.pivot_points(bars["high"], bars["low"], bars["close"]).set_index("level")["price"]
    assert out["r3"] > out["r2"] > out["r1"] > out["pivot"] > out["s1"] > out["s2"] > out["s3"]


def test_supertrend_direction_is_only_plus_or_minus_one(bars):
    out = ta.supertrend(bars["high"], bars["low"], bars["close"])
    assert set(out["supertrend_direction"].unique()) <= {1, -1}


def test_volatility_cones_are_ordered_percentiles(bars):
    out = ta.volatility_cones(bars["close"], windows=(10, 20, 30))
    assert len(out) == 3
    assert (out["min"] <= out["p25"]).all()
    assert (out["p25"] <= out["median"]).all()
    assert (out["median"] <= out["p75"]).all()
    assert (out["p75"] <= out["max"]).all()
