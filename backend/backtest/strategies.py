"""Strategy library.

Each strategy is a callable that consumes a wide *close-price* panel
(index=dates, columns=symbols) and returns a *target-weight* panel of the same
shape, where each row sums (in absolute value) to <= 1. The backtest engine
consumes these weights. Weights are computed using only information available up
to and including each row (the engine additionally lags execution by one bar to
avoid look-ahead).
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

StrategyFn = Callable[[pd.DataFrame, dict], pd.DataFrame]


def _normalize(weights: pd.DataFrame) -> pd.DataFrame:
    """Scale each row so gross exposure (sum |w|) is at most 1."""
    gross = weights.abs().sum(axis=1).replace(0, np.nan)
    scaled = weights.div(gross, axis=0).clip(-1, 1)
    return scaled.fillna(0.0)


def buy_and_hold(prices: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Equal-weight, always invested."""
    w = pd.DataFrame(1.0, index=prices.index, columns=prices.columns)
    return _normalize(w)


def sma_crossover(prices: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Long when fast SMA > slow SMA, flat otherwise. Equal weight across longs."""
    fast = int(params.get("fast", 20))
    slow = int(params.get("slow", 50))
    fast, slow = min(fast, slow), max(fast, slow)
    sma_fast = prices.rolling(fast).mean()
    sma_slow = prices.rolling(slow).mean()
    signal = (sma_fast > sma_slow).astype(float)
    return _normalize(signal)


def cross_sectional_momentum(prices: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Long assets with positive trailing momentum, weighted by signal strength."""
    lookback = int(params.get("lookback", 126))
    skip = int(params.get("skip", 21))
    mom = prices.pct_change(lookback).shift(skip)
    signal = mom.clip(lower=0.0)  # long-only on positive momentum
    return _normalize(signal)


def mean_reversion(prices: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Bollinger-style: long when price is below its band, short when above."""
    window = int(params.get("window", 20))
    z_entry = float(params.get("z", 1.0))
    ma = prices.rolling(window).mean()
    sd = prices.rolling(window).std()
    z = (prices - ma) / sd.replace(0, np.nan)
    # Negative z (cheap) -> long; positive z (rich) -> short.
    signal = (-z).where(z.abs() > z_entry, 0.0)
    return _normalize(signal)


def news_sentiment(prices: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Long a symbol only while its trailing news sentiment reads bullish.

    The signal is the weekly (monthly on multi-year ranges) sentiment series
    rebuilt from the Google News archive and scored with the platform's
    sentiment lexicon — see ``/sentiment/history``. Each window is stamped
    with its END date, so a week's mood is only tradeable once the week has
    closed, and the engine's one-bar execution lag applies on top.

    params: ``threshold`` — score needed to stay invested (default 0.05);
    ``smooth`` — average of the last N windows (default 2).
    """
    threshold = float(params.get("threshold", 0.05))
    smooth = max(1, int(params.get("smooth", 2)))
    # Imported here so the backtest package stays importable without the
    # extensions registry (and to avoid any import-order coupling).
    from ..extensions.sentiment import history_series

    start = prices.index[0].date().isoformat()
    end = prices.index[-1].date().isoformat()
    weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    built = 0
    for sym in prices.columns:
        try:
            series = history_series(sym, start, end)
        except Exception:  # noqa: BLE001 - a symbol without news history sits out
            continue
        if series.empty:
            continue
        signal = series.rolling(smooth, min_periods=1).mean()
        aligned = signal.reindex(prices.index, method="ffill")
        weights[sym] = (aligned > threshold).astype(float)
        built += 1
    if not built:
        raise ValueError(
            "No historical sentiment could be built for the requested symbols/period."
        )
    return _normalize(weights)


REGISTRY: dict[str, StrategyFn] = {
    "buy_and_hold": buy_and_hold,
    "sma_crossover": sma_crossover,
    "momentum": cross_sectional_momentum,
    "mean_reversion": mean_reversion,
    "news_sentiment": news_sentiment,
}


def get_strategy(name: str) -> StrategyFn:
    if name not in REGISTRY:
        raise KeyError(f"Unknown strategy '{name}'. Available: {sorted(REGISTRY)}")
    return REGISTRY[name]
