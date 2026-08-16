"""Performance metrics and report generation.

Given an equity curve / returns series, compute the standard set of risk and
return statistics you'd see on a fund tearsheet, and render a self-contained
HTML report.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _to_returns(equity: pd.Series) -> pd.Series:
    return equity.pct_change().dropna()


def compute_metrics(
    equity: pd.Series, rf_annual: float = 0.0, periods_per_year: int = TRADING_DAYS
) -> dict:
    """Compute performance metrics from an equity curve.

    ``equity`` is a price-like series of portfolio value indexed by date.
    """
    equity = equity.dropna()
    if len(equity) < 2:
        return {"error": "insufficient data"}

    returns = _to_returns(equity)
    rf_period = rf_annual / periods_per_year
    excess = returns - rf_period

    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1)
    n = len(returns)
    years = n / periods_per_year
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) if years > 0 else 0.0

    vol = float(returns.std(ddof=1) * np.sqrt(periods_per_year))
    sharpe = (
        float(excess.mean() / returns.std(ddof=1) * np.sqrt(periods_per_year))
        if returns.std(ddof=1) > 0
        else 0.0
    )

    downside = returns[returns < 0]
    downside_dev = float(downside.std(ddof=1) * np.sqrt(periods_per_year)) if len(downside) > 1 else 0.0
    sortino = float(excess.mean() * periods_per_year / downside_dev) if downside_dev > 0 else 0.0

    # Max drawdown.
    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    max_dd = float(drawdown.min())
    calmar = float(cagr / abs(max_dd)) if max_dd < 0 else 0.0

    win_rate = float((returns > 0).mean())
    best_day = float(returns.max())
    worst_day = float(returns.min())

    return {
        "total_return": round(total_return, 6),
        "cagr": round(cagr, 6),
        "annual_volatility": round(vol, 6),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "calmar": round(calmar, 4),
        "max_drawdown": round(max_dd, 6),
        "win_rate": round(win_rate, 4),
        "best_day": round(best_day, 6),
        "worst_day": round(worst_day, 6),
        "num_periods": int(n),
        "start_value": round(float(equity.iloc[0]), 2),
        "end_value": round(float(equity.iloc[-1]), 2),
    }


def drawdown_series(equity: pd.Series) -> pd.Series:
    running_max = equity.cummax()
    return equity / running_max - 1


def render_html_report(
    title: str, metrics: dict, equity: pd.Series, benchmark: pd.Series | None = None
) -> str:
    """Render a self-contained HTML tearsheet (Chart.js from CDN)."""
    labels = [d.date().isoformat() if hasattr(d, "date") else str(d) for d in equity.index]
    eq_vals = [round(float(v), 2) for v in equity.values]
    dd_vals = [round(float(v) * 100, 3) for v in drawdown_series(equity).values]
    bench_vals = (
        [round(float(v), 2) for v in benchmark.reindex(equity.index).ffill().values]
        if benchmark is not None
        else None
    )

    def metric_rows() -> str:
        order = [
            ("Total Return", "total_return", "pct"),
            ("CAGR", "cagr", "pct"),
            ("Sharpe Ratio", "sharpe", "num"),
            ("Sortino Ratio", "sortino", "num"),
            ("Calmar Ratio", "calmar", "num"),
            ("Annual Volatility", "annual_volatility", "pct"),
            ("Max Drawdown", "max_drawdown", "pct"),
            ("Win Rate", "win_rate", "pct"),
        ]
        rows = []
        for label, key, fmt in order:
            if key not in metrics:
                continue
            v = metrics[key]
            disp = f"{v * 100:.2f}%" if fmt == "pct" else f"{v:.2f}"
            rows.append(f"<tr><td>{label}</td><td class='val'>{disp}</td></tr>")
        return "\n".join(rows)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bench_dataset = (
        f"""{{label:'Benchmark',data:{bench_vals},borderColor:'#888',
              borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false}},"""
        if bench_vals
        else ""
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 body{{background:#0b0e11;color:#d6e1e8;font-family:'SFMono-Regular',Consolas,monospace;margin:0;padding:24px}}
 h1{{color:#f5a623;font-size:18px;border-bottom:1px solid #1d2530;padding-bottom:8px}}
 .meta{{color:#5c6b7a;font-size:12px;margin-bottom:20px}}
 table{{border-collapse:collapse;width:320px;margin-bottom:24px}}
 td{{padding:6px 10px;border-bottom:1px solid #1d2530;font-size:13px}}
 .val{{text-align:right;color:#3fb950;font-weight:600}}
 .chart{{background:#11161c;border:1px solid #1d2530;border-radius:6px;padding:12px;margin-bottom:20px}}
</style></head>
<body>
 <h1>&#9632; {title}</h1>
 <div class="meta">Generated {generated} &middot; {metrics.get('num_periods', 0)} periods</div>
 <table>{metric_rows()}</table>
 <div class="chart"><canvas id="equity" height="90"></canvas></div>
 <div class="chart"><canvas id="dd" height="60"></canvas></div>
<script>
const labels={labels};
new Chart(document.getElementById('equity'),{{type:'line',
 data:{{labels,datasets:[
   {{label:'Strategy',data:{eq_vals},borderColor:'#3fb950',borderWidth:1.5,pointRadius:0,fill:false}},
   {bench_dataset}
 ]}},
 options:{{plugins:{{legend:{{labels:{{color:'#d6e1e8'}}}}}},
   scales:{{x:{{ticks:{{color:'#5c6b7a',maxTicksLimit:8}}}},y:{{ticks:{{color:'#5c6b7a'}}}}}}}}}});
new Chart(document.getElementById('dd'),{{type:'line',
 data:{{labels,datasets:[{{label:'Drawdown %',data:{dd_vals},borderColor:'#f85149',
   backgroundColor:'rgba(248,81,73,.15)',borderWidth:1,pointRadius:0,fill:true}}]}},
 options:{{plugins:{{legend:{{labels:{{color:'#d6e1e8'}}}}}},
   scales:{{x:{{ticks:{{color:'#5c6b7a',maxTicksLimit:8}}}},y:{{ticks:{{color:'#5c6b7a'}}}}}}}}}});
</script>
</body></html>"""
