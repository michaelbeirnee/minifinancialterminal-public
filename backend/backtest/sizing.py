"""Position-sizing overlays.

These sit between a strategy's raw target weights and the engine: they take the
same wide (dates x symbols) weight panel a strategy emits and return a modified
panel, so they compose with any strategy and either engine. Like strategies,
each row uses only information available up to and including that row — the
engine's one-bar execution lag still applies on top.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def apply_vol_target(
    prices: pd.DataFrame,
    weights: pd.DataFrame,
    target_vol: float = 0.15,
    lookback: int = 20,
    max_leverage: float = 2.0,
) -> pd.DataFrame:
    """Scale each day's weights so trailing realized vol matches ``target_vol``.

    Realized vol is measured on the strategy's own (lagged-weight) daily
    returns over ``lookback`` days. Scaling can lever the book above gross 1.0,
    capped at ``max_leverage``; no borrow cost is modeled. During warm-up, or
    when the book is flat, weights pass through unscaled.
    """
    weights = weights.reindex(prices.index).ffill().fillna(0.0)
    asset_returns = prices.pct_change().fillna(0.0)
    held = weights.shift(1).fillna(0.0)
    strat_returns = (held * asset_returns).sum(axis=1)

    realized = strat_returns.rolling(lookback).std(ddof=1) * np.sqrt(TRADING_DAYS)
    leverage = (target_vol / realized.replace(0.0, np.nan)).clip(upper=max_leverage)
    leverage = leverage.fillna(1.0)
    return weights.mul(leverage, axis=0)


def apply_stop_loss(
    prices: pd.DataFrame,
    weights: pd.DataFrame,
    stop_pct: float = 0.10,
    trailing: bool = True,
) -> pd.DataFrame:
    """Zero out a position once it loses ``stop_pct`` from its reference price.

    The reference is the best price since entry when ``trailing``, else the
    entry price. Entry/exit are read off the signal panel: a position "entry"
    is the bar the raw weight's sign changes. Once stopped, the symbol stays
    flat until the strategy's raw signal closes or flips — re-entry then arms a
    fresh stop.
    """
    weights = weights.reindex(prices.index).ffill().fillna(0.0)
    px = prices.reindex(weights.index).ffill()
    out = weights.copy()

    for col in weights.columns:
        w = weights[col].to_numpy()
        p = px[col].to_numpy(dtype=float)
        keep = np.ones(len(w), dtype=bool)
        stopped = False
        entry = np.nan
        extreme = np.nan
        prev_sign = 0
        for i in range(len(w)):
            sign = 0 if w[i] == 0 else (1 if w[i] > 0 else -1)
            if sign != prev_sign:
                stopped = False
                entry = extreme = p[i]
            elif sign != 0 and np.isfinite(p[i]):
                if not np.isfinite(extreme):
                    entry = extreme = p[i]
                elif not stopped:
                    extreme = max(extreme, p[i]) if sign > 0 else min(extreme, p[i])
            if sign != 0 and not stopped and np.isfinite(p[i]):
                ref = extreme if trailing else entry
                if np.isfinite(ref) and ref > 0:
                    loss = (p[i] / ref - 1) * sign  # negative when losing
                    if loss <= -stop_pct:
                        stopped = True
            if stopped:
                keep[i] = False
            prev_sign = sign
        out[col] = np.where(keep, w, 0.0)

    return out
