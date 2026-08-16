"""Backtesting engines.

Two complementary engines share the same cost model:

* :class:`VectorizedBacktester` — fast, weight-based daily rebalancing. Ideal
  for sweeping strategy parameters (see :func:`backend.backtest.analysis.sweep`
  and :func:`~backend.backtest.analysis.walk_forward`, which drive it).
* :class:`EventDrivenEngine` — bar-by-bar simulation with explicit cash,
  positions, commission, slippage and execution latency. This is the engine
  used for higher-frequency / intraday studies where path dependence and
  fill mechanics matter.

Both apply a one-bar execution lag so signals computed from a bar are only
acted upon at the *next* bar, eliminating look-ahead bias.

Research workflows built on the engines live in sibling modules:
:mod:`~backend.backtest.analysis` (parameter sweeps, walk-forward evaluation,
benchmark attribution, cost sensitivity, Monte Carlo) and
:mod:`~backend.backtest.sizing` (vol targeting and stop-loss overlays, applied
between strategy and engine via :func:`run_backtest`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..reports.generator import compute_metrics
from .sizing import apply_stop_loss, apply_vol_target
from .strategies import get_strategy


@dataclass
class CostModel:
    commission_bps: float = 1.0   # per-side commission, basis points of notional
    slippage_bps: float = 2.0     # market-impact / spread, basis points

    @property
    def per_trade_rate(self) -> float:
        return (self.commission_bps + self.slippage_bps) / 1e4


@dataclass
class BacktestResult:
    equity: pd.Series
    returns: pd.Series
    weights: pd.DataFrame
    metrics: dict
    turnover: float
    total_costs: float
    engine: str
    trades: list = field(default_factory=list)

    def to_dict(self, max_points: int = 1500, max_trades: int = 200) -> dict:
        eq = self.equity
        if len(eq) > max_points:  # downsample for transport
            step = len(eq) // max_points + 1
            eq = eq.iloc[::step]
        return {
            "engine": self.engine,
            "metrics": self.metrics,
            "turnover": round(self.turnover, 4),
            "total_costs": round(self.total_costs, 6),
            "equity_curve": {
                "dates": [
                    d.date().isoformat() if hasattr(d, "date") else str(d) for d in eq.index
                ],
                "values": [round(float(v), 4) for v in eq.values],
            },
            "num_trades": len(self.trades),
            # Most recent fills; the full log stays on the result object.
            "trades": self.trades[-max_trades:],
            "trades_truncated": len(self.trades) > max_trades,
        }


class VectorizedBacktester:
    def __init__(self, cost_model: CostModel | None = None, initial_capital: float = 100_000.0):
        self.costs = cost_model or CostModel()
        self.initial_capital = initial_capital

    def run(self, prices: pd.DataFrame, weights: pd.DataFrame) -> BacktestResult:
        prices = prices.sort_index()
        asset_returns = prices.pct_change().fillna(0.0)

        # Execution lag: hold yesterday's target weights through today.
        held = weights.reindex(prices.index).ffill().shift(1).fillna(0.0)

        gross_returns = (held * asset_returns).sum(axis=1)

        # Transaction costs from turnover (sum of absolute weight changes).
        turnover_per_day = held.diff().abs().sum(axis=1).fillna(0.0)
        daily_costs = turnover_per_day * self.costs.per_trade_rate
        net_returns = gross_returns - daily_costs

        equity = self.initial_capital * (1 + net_returns).cumprod()
        metrics = compute_metrics(equity)

        return BacktestResult(
            equity=equity,
            returns=net_returns,
            weights=held,
            metrics=metrics,
            turnover=float(turnover_per_day.sum()),
            total_costs=float((self.initial_capital * daily_costs).sum()),
            engine="vectorized",
        )


class EventDrivenEngine:
    """Bar-by-bar simulation with explicit fills, cash and latency.

    ``latency_bars`` controls how many bars elapse between signal generation and
    execution (>=1). Fills occur at the next bar's open, adjusted for slippage.
    """

    def __init__(
        self,
        cost_model: CostModel | None = None,
        initial_capital: float = 100_000.0,
        latency_bars: int = 1,
    ):
        self.costs = cost_model or CostModel()
        self.initial_capital = initial_capital
        self.latency = max(1, latency_bars)

    def run(self, prices: pd.DataFrame, weights: pd.DataFrame, opens: pd.DataFrame | None = None) -> BacktestResult:
        prices = prices.sort_index()
        symbols = list(prices.columns)
        # Execute at next-bar open when available, else close.
        fill_px = (opens if opens is not None else prices).reindex(prices.index).ffill()

        target = weights.reindex(prices.index).ffill().fillna(0.0)

        cash = self.initial_capital
        positions = {s: 0.0 for s in symbols}
        equity_curve = []
        trades: list[dict] = []
        total_costs = 0.0
        total_turnover = 0.0

        dates = list(prices.index)
        for i, dt in enumerate(dates):
            mark = prices.loc[dt]
            nav = cash + sum(positions[s] * mark[s] for s in symbols)

            # Act on the signal from ``latency`` bars ago.
            sig_idx = i - self.latency
            if sig_idx >= 0 and nav > 0:
                tgt_w = target.iloc[sig_idx]
                for s in symbols:
                    px = float(fill_px.loc[dt, s])
                    if not np.isfinite(px) or px <= 0:
                        continue
                    target_value = float(tgt_w[s]) * nav
                    target_shares = target_value / px
                    delta = target_shares - positions[s]
                    if abs(delta * px) < nav * 1e-4:  # ignore dust trades
                        continue
                    side = 1 if delta > 0 else -1
                    fill = px * (1 + side * self.costs.slippage_bps / 1e4)
                    notional = abs(delta) * fill
                    commission = notional * self.costs.commission_bps / 1e4
                    cash -= delta * fill + commission
                    positions[s] = target_shares
                    total_costs += commission + abs(delta) * abs(fill - px)
                    total_turnover += notional
                    trades.append(
                        {
                            "date": dt.date().isoformat() if hasattr(dt, "date") else str(dt),
                            "symbol": s,
                            "shares": round(delta, 4),
                            "price": round(fill, 4),
                            "side": "BUY" if side > 0 else "SELL",
                        }
                    )

            nav = cash + sum(positions[s] * mark[s] for s in symbols)
            equity_curve.append(nav)

        equity = pd.Series(equity_curve, index=prices.index)
        returns = equity.pct_change().fillna(0.0)
        metrics = compute_metrics(equity)

        return BacktestResult(
            equity=equity,
            returns=returns,
            weights=target,
            metrics=metrics,
            turnover=round(total_turnover / self.initial_capital, 4),
            total_costs=total_costs,
            engine="event_driven",
            trades=trades,
        )


def run_backtest(
    prices: pd.DataFrame,
    strategy: str,
    params: dict | None = None,
    engine: str = "vectorized",
    commission_bps: float = 1.0,
    slippage_bps: float = 2.0,
    initial_capital: float = 100_000.0,
    opens: pd.DataFrame | None = None,
    vol_target: float | None = None,
    vol_lookback: int = 20,
    max_leverage: float = 2.0,
    stop_loss: float | None = None,
    trailing_stop: bool = True,
) -> BacktestResult:
    """High-level entry point: build signals from ``strategy`` and simulate.

    Optional position-sizing overlays run between strategy and engine: a
    stop-loss (``stop_loss`` as a fraction, trailing by default) first, then
    vol targeting (``vol_target`` as annualized vol, levered up to
    ``max_leverage``).
    """
    params = params or {}
    weights = get_strategy(strategy)(prices, params)
    if stop_loss is not None:
        weights = apply_stop_loss(prices, weights, stop_pct=stop_loss, trailing=trailing_stop)
    if vol_target is not None:
        weights = apply_vol_target(
            prices, weights, target_vol=vol_target, lookback=vol_lookback, max_leverage=max_leverage
        )
    cost_model = CostModel(commission_bps=commission_bps, slippage_bps=slippage_bps)

    if engine == "event_driven":
        return EventDrivenEngine(cost_model, initial_capital).run(prices, weights, opens)
    return VectorizedBacktester(cost_model, initial_capital).run(prices, weights)
