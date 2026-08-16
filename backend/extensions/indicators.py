"""Technical indicator library.

Implemented directly on pandas/numpy rather than pulling in a TA wrapper: the
formulas are short, the behaviour is explicit (Wilder smoothing where Wilder
intended it), and there is no third-party package to go stale against numpy.

Every function takes and returns pandas objects indexed by date.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #
def sma(series: pd.Series, length: int = 20) -> pd.Series:
    return series.rolling(length).mean().rename("sma_{}".format(length))


def ema(series: pd.Series, length: int = 20) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean().rename("ema_{}".format(length))


def wma(series: pd.Series, length: int = 20) -> pd.Series:
    weights = np.arange(1, length + 1, dtype=float)
    out = series.rolling(length).apply(lambda w: np.dot(w, weights) / weights.sum(), raw=True)
    return out.rename("wma_{}".format(length))


def hma(series: pd.Series, length: int = 20) -> pd.Series:
    """Hull moving average — WMA of (2*WMA(n/2) - WMA(n)) over sqrt(n)."""
    half = max(int(length / 2), 1)
    root = max(int(np.sqrt(length)), 1)
    raw = 2 * wma(series, half) - wma(series, length)
    return wma(raw, root).rename("hma_{}".format(length))


def zlma(series: pd.Series, length: int = 20) -> pd.Series:
    """Zero-lag EMA: EMA of price shifted forward by half the window."""
    lag = int((length - 1) / 2)
    return ema(2 * series - series.shift(lag), length).rename("zlma_{}".format(length))


def dema(series: pd.Series, length: int = 20) -> pd.Series:
    e1 = series.ewm(span=length, adjust=False).mean()
    e2 = e1.ewm(span=length, adjust=False).mean()
    return (2 * e1 - e2).rename("dema_{}".format(length))


def tema(series: pd.Series, length: int = 20) -> pd.Series:
    e1 = series.ewm(span=length, adjust=False).mean()
    e2 = e1.ewm(span=length, adjust=False).mean()
    e3 = e2.ewm(span=length, adjust=False).mean()
    return (3 * e1 - 3 * e2 + e3).rename("tema_{}".format(length))


def _wilder(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing (an EMA with alpha = 1/n)."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


# --------------------------------------------------------------------------- #
# Momentum
# --------------------------------------------------------------------------- #
def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = _wilder(delta.clip(lower=0), length)
    loss = _wilder((-delta).clip(lower=0), length)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # A window with no down-closes divides by zero: it is fully overbought.
    # A window that never moved carries no information, so it sits mid-range.
    out = out.mask((loss == 0) & (gain > 0), 100.0)
    out = out.mask((loss == 0) & (gain == 0), 50.0)
    return out.rename("rsi_{}".format(length))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(series, fast) - ema(series, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_histogram": line - sig})


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
               length: int = 14, smooth: int = 3) -> pd.DataFrame:
    lowest = low.rolling(length).min()
    highest = high.rolling(length).max()
    k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    return pd.DataFrame({"stoch_k": k, "stoch_d": k.rolling(smooth).mean()})


def cci(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 20) -> pd.Series:
    tp = (high + low + close) / 3
    mean_dev = tp.rolling(length).apply(lambda w: np.abs(w - w.mean()).mean(), raw=True)
    return ((tp - tp.rolling(length).mean()) / (0.015 * mean_dev)).rename("cci_{}".format(length))


def williams_r(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    highest = high.rolling(length).max()
    lowest = low.rolling(length).min()
    return (-100 * (highest - close) / (highest - lowest).replace(0, np.nan)).rename("williams_r")


def roc(series: pd.Series, length: int = 10) -> pd.Series:
    return (series.pct_change(length) * 100).rename("roc_{}".format(length))


def ppo(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = 100 * (ema(series, fast) - ema(series, slow)) / ema(series, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"ppo": line, "ppo_signal": sig, "ppo_histogram": line - sig})


def tsi(series: pd.Series, long: int = 25, short: int = 13) -> pd.Series:
    diff = series.diff()
    smooth = diff.ewm(span=long, adjust=False).mean().ewm(span=short, adjust=False).mean()
    abs_smooth = diff.abs().ewm(span=long, adjust=False).mean().ewm(span=short, adjust=False).mean()
    return (100 * smooth / abs_smooth.replace(0, np.nan)).rename("tsi")


def ultimate_oscillator(high: pd.Series, low: pd.Series, close: pd.Series,
                        short: int = 7, medium: int = 14, long: int = 28) -> pd.Series:
    prior_close = close.shift(1)
    true_low = pd.concat([low, prior_close], axis=1).min(axis=1)
    buying_pressure = close - true_low
    true_range = pd.concat([high, prior_close], axis=1).max(axis=1) - true_low
    def avg(n: int) -> pd.Series:
        return buying_pressure.rolling(n).sum() / true_range.rolling(n).sum().replace(0, np.nan)
    out = 100 * (4 * avg(short) + 2 * avg(medium) + avg(long)) / 7
    return out.rename("ultimate_oscillator")


def fisher_transform(high: pd.Series, low: pd.Series, length: int = 9) -> pd.DataFrame:
    median = (high + low) / 2
    lowest = median.rolling(length).min()
    highest = median.rolling(length).max()
    raw = 2 * (median - lowest) / (highest - lowest).replace(0, np.nan) - 1
    value = raw.clip(-0.999, 0.999).ewm(alpha=0.33, adjust=False).mean()
    fish = 0.5 * np.log((1 + value) / (1 - value))
    fish = fish.ewm(alpha=0.5, adjust=False).mean()
    return pd.DataFrame({"fisher": fish, "fisher_signal": fish.shift(1)})


def center_of_gravity(series: pd.Series, length: int = 10) -> pd.Series:
    """Ehlers' Center of Gravity oscillator."""
    weights = np.arange(1, length + 1, dtype=float)

    def _cg(window: np.ndarray) -> float:
        rev = window[::-1]
        total = rev.sum()
        return -np.dot(rev, weights) / total if total else np.nan

    return series.rolling(length).apply(_cg, raw=True).rename("cg_{}".format(length))


def demark_sequential(close: pd.Series) -> pd.DataFrame:
    """TD setup counts: consecutive closes above/below the close four bars back."""
    up = (close > close.shift(4)).astype(int)
    down = (close < close.shift(4)).astype(int)

    def _streak(flags: pd.Series) -> pd.Series:
        out, run = [], 0
        for flag in flags:
            run = run + 1 if flag else 0
            out.append(min(run, 9))
        return pd.Series(out, index=flags.index)

    return pd.DataFrame({"td_buy_setup": _streak(down), "td_sell_setup": _streak(up)})


# --------------------------------------------------------------------------- #
# Volatility
# --------------------------------------------------------------------------- #
def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prior = close.shift(1)
    return pd.concat([high - low, (high - prior).abs(), (low - prior).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    return _wilder(true_range(high, low, close), length).rename("atr_{}".format(length))


def bollinger(series: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = series.rolling(length).mean()
    dev = series.rolling(length).std(ddof=0)
    upper, lower = mid + std * dev, mid - std * dev
    return pd.DataFrame(
        {
            "bb_lower": lower, "bb_middle": mid, "bb_upper": upper,
            "bb_width": (upper - lower) / mid.replace(0, np.nan),
            "bb_percent": (series - lower) / (upper - lower).replace(0, np.nan),
        }
    )


def keltner(high: pd.Series, low: pd.Series, close: pd.Series,
            length: int = 20, multiplier: float = 2.0) -> pd.DataFrame:
    mid = ema(close, length)
    band = multiplier * atr(high, low, close, length)
    return pd.DataFrame({"kc_lower": mid - band, "kc_middle": mid, "kc_upper": mid + band})


def donchian(high: pd.Series, low: pd.Series, length: int = 20) -> pd.DataFrame:
    upper = high.rolling(length).max()
    lower = low.rolling(length).min()
    return pd.DataFrame({"dc_lower": lower, "dc_middle": (upper + lower) / 2, "dc_upper": upper})


def volatility_cones(close: pd.Series, windows: Tuple[int, ...] = (10, 20, 30, 60, 90, 120),
                     trading_days: int = 252) -> pd.DataFrame:
    """Realised-volatility percentiles per window — the classic cone chart."""
    log_ret = np.log(close / close.shift(1))
    rows = []
    for window in windows:
        realised = log_ret.rolling(window).std(ddof=1) * np.sqrt(trading_days)
        realised = realised.dropna()
        if realised.empty:
            continue
        rows.append(
            {
                "window": window,
                "min": float(realised.min()),
                "p25": float(realised.quantile(0.25)),
                "median": float(realised.median()),
                "p75": float(realised.quantile(0.75)),
                "max": float(realised.max()),
                "realised": float(realised.iloc[-1]),
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #
def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    return (np.sign(close.diff().fillna(0)) * volume).cumsum().rename("obv")


def accumulation_distribution(high: pd.Series, low: pd.Series, close: pd.Series,
                              volume: pd.Series) -> pd.Series:
    span = (high - low).replace(0, np.nan)
    clv = ((close - low) - (high - close)) / span
    return (clv.fillna(0) * volume).cumsum().rename("ad")


def adosc(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
          fast: int = 3, slow: int = 10) -> pd.Series:
    line = accumulation_distribution(high, low, close, volume)
    return (ema(line, fast) - ema(line, slow)).rename("adosc")


def chaikin_money_flow(high: pd.Series, low: pd.Series, close: pd.Series,
                       volume: pd.Series, length: int = 20) -> pd.Series:
    span = (high - low).replace(0, np.nan)
    mfv = (((close - low) - (high - close)) / span).fillna(0) * volume
    return (mfv.rolling(length).sum() / volume.rolling(length).sum().replace(0, np.nan)).rename("cmf")


def money_flow_index(high: pd.Series, low: pd.Series, close: pd.Series,
                     volume: pd.Series, length: int = 14) -> pd.Series:
    tp = (high + low + close) / 3
    flow = tp * volume
    direction = tp.diff()
    positive = flow.where(direction > 0, 0.0).rolling(length).sum()
    negative = flow.where(direction < 0, 0.0).rolling(length).sum()
    ratio = positive / negative.replace(0, np.nan)
    return (100 - 100 / (1 + ratio)).rename("mfi_{}".format(length))


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
         anchor: Optional[str] = None) -> pd.Series:
    """Volume-weighted average price, optionally re-anchored (``D``, ``W``, ``M``)."""
    tp = (high + low + close) / 3
    if anchor:
        groups = pd.Series(close.index, index=close.index).dt.to_period(anchor)
        cum_pv = (tp * volume).groupby(groups).cumsum()
        cum_v = volume.groupby(groups).cumsum()
    else:
        cum_pv, cum_v = (tp * volume).cumsum(), volume.cumsum()
    return (cum_pv / cum_v.replace(0, np.nan)).rename("vwap")


# --------------------------------------------------------------------------- #
# Trend strength & structure
# --------------------------------------------------------------------------- #
def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.DataFrame:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = _wilder(true_range(high, low, close), length)
    plus_di = 100 * _wilder(plus_dm, length) / tr.replace(0, np.nan)
    minus_di = 100 * _wilder(minus_dm, length) / tr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": _wilder(dx, length)})


def aroon(high: pd.Series, low: pd.Series, length: int = 25) -> pd.DataFrame:
    # Bars since the extreme, measured over a window of length+1 bars.
    up = high.rolling(length + 1).apply(lambda w: 100 * int(np.argmax(w)) / length, raw=True)
    down = low.rolling(length + 1).apply(lambda w: 100 * int(np.argmin(w)) / length, raw=True)
    return pd.DataFrame({"aroon_up": up, "aroon_down": down, "aroon_oscillator": up - down})


def parabolic_sar(high: pd.Series, low: pd.Series, step: float = 0.02,
                  maximum: float = 0.2) -> pd.Series:
    values = np.full(len(high), np.nan)
    if len(high) < 2:
        return pd.Series(values, index=high.index, name="psar")
    long = True
    af = step
    sar = low.iloc[0]
    ep = high.iloc[0]
    highs, lows = high.to_numpy(), low.to_numpy()
    for i in range(1, len(highs)):
        sar = sar + af * (ep - sar)
        if long:
            sar = min(sar, lows[i - 1], lows[max(i - 2, 0)])
            if lows[i] < sar:
                long, sar, ep, af = False, ep, lows[i], step
            elif highs[i] > ep:
                ep, af = highs[i], min(af + step, maximum)
        else:
            sar = max(sar, highs[i - 1], highs[max(i - 2, 0)])
            if highs[i] > sar:
                long, sar, ep, af = True, ep, highs[i], step
            elif lows[i] < ep:
                ep, af = lows[i], min(af + step, maximum)
        values[i] = sar
    return pd.Series(values, index=high.index, name="psar")


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               length: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    band = multiplier * atr(high, low, close, length)
    mid = (high + low) / 2
    upper, lower = mid + band, mid - band
    closes = close.to_numpy()
    ups, lows = upper.to_numpy(), lower.to_numpy()
    trend, direction = [], []
    prev_upper = prev_lower = np.nan
    prev_dir = 1
    for i in range(len(closes)):
        u, l = ups[i], lows[i]
        if i and not np.isnan(prev_upper):
            # Bands only ratchet inward while price stays on the same side.
            u = min(u, prev_upper) if closes[i - 1] <= prev_upper else u
            l = max(l, prev_lower) if closes[i - 1] >= prev_lower else l
            prev_dir = 1 if closes[i] > prev_upper else (-1 if closes[i] < prev_lower else prev_dir)
        direction.append(prev_dir)
        trend.append(l if prev_dir == 1 else u)
        prev_upper, prev_lower = u, l
    return pd.DataFrame({"supertrend": trend, "supertrend_direction": direction}, index=close.index)


def ichimoku(high: pd.Series, low: pd.Series, close: pd.Series,
             conversion: int = 9, base: int = 26, span_b: int = 52) -> pd.DataFrame:
    def mid(n: int) -> pd.Series:
        return (high.rolling(n).max() + low.rolling(n).min()) / 2

    tenkan, kijun = mid(conversion), mid(base)
    return pd.DataFrame(
        {
            "tenkan_sen": tenkan,
            "kijun_sen": kijun,
            "senkou_span_a": ((tenkan + kijun) / 2).shift(base),
            "senkou_span_b": mid(span_b).shift(base),
            "chikou_span": close.shift(-base),
        }
    )


def fibonacci_levels(high: pd.Series, low: pd.Series) -> pd.DataFrame:
    top, bottom = float(high.max()), float(low.min())
    span = top - bottom
    ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618]
    return pd.DataFrame(
        [{"ratio": r, "retracement": top - span * r, "extension": bottom + span * r} for r in ratios]
    )


def pivot_points(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.DataFrame:
    h, l, c = float(high.iloc[-1]), float(low.iloc[-1]), float(close.iloc[-1])
    p = (h + l + c) / 3
    return pd.DataFrame(
        [
            {"level": "r3", "price": h + 2 * (p - l)},
            {"level": "r2", "price": p + (h - l)},
            {"level": "r1", "price": 2 * p - l},
            {"level": "pivot", "price": p},
            {"level": "s1", "price": 2 * p - h},
            {"level": "s2", "price": p - (h - l)},
            {"level": "s3", "price": l - 2 * (h - p)},
        ]
    )


# --------------------------------------------------------------------------- #
# Statistical / cross-sectional
# --------------------------------------------------------------------------- #
def hurst_exponent(series: pd.Series, min_lag: int = 2, max_lag: int = 100) -> float:
    """Rescaled-range style estimate; >0.5 trending, <0.5 mean-reverting."""
    values = np.log(series.dropna().to_numpy())
    max_lag = min(max_lag, len(values) // 2)
    if max_lag <= min_lag:
        return float("nan")
    lags = np.arange(min_lag, max_lag)
    tau = [np.std(values[lag:] - values[:-lag]) for lag in lags]
    tau = np.array(tau)
    mask = tau > 0
    if mask.sum() < 2:
        return float("nan")
    slope = np.polyfit(np.log(lags[mask]), np.log(tau[mask]), 1)[0]
    return float(slope)


def clenow_momentum(close: pd.Series, length: int = 90) -> Dict[str, float]:
    """Annualised exponential-regression slope scaled by R² (Clenow's ranking)."""
    window = close.dropna().tail(length)
    if len(window) < max(length // 2, 10):
        return {"annualised_slope": float("nan"), "r_squared": float("nan"), "score": float("nan")}
    y = np.log(window.to_numpy())
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_res = float(((y - fitted) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else float("nan")
    annualised = (np.exp(slope) ** 252 - 1) * 100
    return {"annualised_slope": float(annualised), "r_squared": float(r2),
            "score": float(annualised * r2)}


def relative_strength(close: pd.Series, benchmark: pd.Series, length: int = 63) -> pd.DataFrame:
    aligned = pd.concat([close.rename("asset"), benchmark.rename("benchmark")], axis=1).dropna()
    ratio = aligned["asset"] / aligned["benchmark"]
    return pd.DataFrame(
        {
            "ratio": ratio,
            "ratio_sma": ratio.rolling(length).mean(),
            "relative_return": aligned["asset"].pct_change(length) - aligned["benchmark"].pct_change(length),
        }
    )
