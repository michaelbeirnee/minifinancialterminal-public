"""Technical menu — indicators computed on freshly fetched price history.

Every command takes a symbol plus the usual date window, so they work from the
REST API and the CLI without having to POST a price series first.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, one_symbol
from ..providers import markets, yahoo
from . import indicators as ta

_PROVIDERS = ("yahoo", "stooq")


def _prices(symbol: str, start_date: Optional[str], end_date: Optional[str],
            interval: str = "1d", provider: Optional[str] = None) -> "tuple":
    src = resolve_provider(provider, _PROVIDERS)
    sym = one_symbol(symbol)
    start, end = date_window(start_date, end_date)
    df = (markets.stooq_history(sym, str(start), str(end)) if src == "stooq"
          else yahoo.history(sym, str(start), str(end), interval=interval))
    if df.empty:
        raise EmptyDataError("No price history for {}".format(sym))
    return df, sym, src


def _emit(df: pd.DataFrame, extra: pd.DataFrame, symbol: str, src: str, limit: int) -> Result:
    out = pd.concat([df[["close"]], extra], axis=1)
    out.insert(0, "symbol", symbol)
    return Result(out.tail(limit), provider=src, index_name="date")


# --------------------------------------------------------------------------- #
# Moving-average family (identical signature, generated)
# --------------------------------------------------------------------------- #
_MA_FUNCS = {
    "sma": (ta.sma, "Simple moving average"),
    "ema": (ta.ema, "Exponential moving average"),
    "wma": (ta.wma, "Weighted moving average"),
    "hma": (ta.hma, "Hull moving average"),
    "zlma": (ta.zlma, "Zero-lag moving average"),
    "dema": (ta.dema, "Double exponential moving average"),
    "tema": (ta.tema, "Triple exponential moving average"),
}


def _make_ma(name: str, func: Callable[..., pd.Series]):
    def fn(symbol: str, length: int = 20, start_date: Optional[str] = None,
           end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
           provider: Optional[str] = None) -> Result:
        df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
        return _emit(df, func(df["close"], length).to_frame(), sym, src, limit)

    fn.__name__ = "technical_" + name
    return fn


for _name, (_func, _desc) in _MA_FUNCS.items():
    command("/technical/" + _name, providers=_PROVIDERS, summary=_desc)(_make_ma(_name, _func))


# --------------------------------------------------------------------------- #
# Momentum
# --------------------------------------------------------------------------- #
@command("/technical/rsi", providers=_PROVIDERS, summary="Relative strength index (Wilder)")
def rsi(symbol: str, length: int = 14, start_date: Optional[str] = None,
        end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
        provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.rsi(df["close"], length).to_frame(), sym, src, limit)


@command("/technical/macd", providers=_PROVIDERS, summary="MACD line, signal and histogram")
def macd(symbol: str, fast: int = 12, slow: int = 26, signal: int = 9,
         start_date: Optional[str] = None, end_date: Optional[str] = None,
         interval: str = "1d", limit: int = 500, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.macd(df["close"], fast, slow, signal), sym, src, limit)


@command("/technical/stoch", providers=_PROVIDERS, summary="Stochastic oscillator")
def stoch(symbol: str, length: int = 14, smooth: int = 3, start_date: Optional[str] = None,
          end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
          provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.stochastic(df["high"], df["low"], df["close"], length, smooth), sym, src, limit)


@command("/technical/cci", providers=_PROVIDERS, summary="Commodity channel index")
def cci(symbol: str, length: int = 20, start_date: Optional[str] = None,
        end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
        provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.cci(df["high"], df["low"], df["close"], length).to_frame(), sym, src, limit)


@command("/technical/williams_r", providers=_PROVIDERS, summary="Williams %R")
def williams_r(symbol: str, length: int = 14, start_date: Optional[str] = None,
               end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
               provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.williams_r(df["high"], df["low"], df["close"], length).to_frame(),
                 sym, src, limit)


@command("/technical/roc", providers=_PROVIDERS, summary="Rate of change")
def roc(symbol: str, length: int = 10, start_date: Optional[str] = None,
        end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
        provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.roc(df["close"], length).to_frame(), sym, src, limit)


@command("/technical/ppo", providers=_PROVIDERS, summary="Percentage price oscillator")
def ppo(symbol: str, fast: int = 12, slow: int = 26, signal: int = 9,
        start_date: Optional[str] = None, end_date: Optional[str] = None,
        interval: str = "1d", limit: int = 500, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.ppo(df["close"], fast, slow, signal), sym, src, limit)


@command("/technical/tsi", providers=_PROVIDERS, summary="True strength index")
def tsi(symbol: str, long: int = 25, short: int = 13, start_date: Optional[str] = None,
        end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
        provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.tsi(df["close"], long, short).to_frame(), sym, src, limit)


@command("/technical/ultimate_oscillator", providers=_PROVIDERS, summary="Ultimate oscillator")
def ultimate(symbol: str, short: int = 7, medium: int = 14, long: int = 28,
             start_date: Optional[str] = None, end_date: Optional[str] = None,
             interval: str = "1d", limit: int = 500, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.ultimate_oscillator(df["high"], df["low"], df["close"], short, medium, long)
                 .to_frame(), sym, src, limit)


@command("/technical/fisher", providers=_PROVIDERS, summary="Fisher transform")
def fisher(symbol: str, length: int = 9, start_date: Optional[str] = None,
           end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
           provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.fisher_transform(df["high"], df["low"], length), sym, src, limit)


@command("/technical/cg", providers=_PROVIDERS, summary="Ehlers centre-of-gravity oscillator")
def cg(symbol: str, length: int = 10, start_date: Optional[str] = None,
       end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
       provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.center_of_gravity(df["close"], length).to_frame(), sym, src, limit)


@command("/technical/demark", providers=_PROVIDERS, summary="DeMark TD sequential setup counts")
def demark(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
           interval: str = "1d", limit: int = 500, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.demark_sequential(df["close"]), sym, src, limit)


# --------------------------------------------------------------------------- #
# Volatility
# --------------------------------------------------------------------------- #
@command("/technical/atr", providers=_PROVIDERS, summary="Average true range")
def atr(symbol: str, length: int = 14, start_date: Optional[str] = None,
        end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
        provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.atr(df["high"], df["low"], df["close"], length).to_frame(), sym, src, limit)


@command("/technical/bbands", providers=_PROVIDERS, summary="Bollinger bands, width and %B")
def bbands(symbol: str, length: int = 20, std: float = 2.0, start_date: Optional[str] = None,
           end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
           provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.bollinger(df["close"], length, std), sym, src, limit)


@command("/technical/kc", providers=_PROVIDERS, summary="Keltner channels")
def kc(symbol: str, length: int = 20, multiplier: float = 2.0, start_date: Optional[str] = None,
       end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
       provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.keltner(df["high"], df["low"], df["close"], length, multiplier),
                 sym, src, limit)


@command("/technical/donchian", providers=_PROVIDERS, summary="Donchian channels")
def donchian(symbol: str, length: int = 20, start_date: Optional[str] = None,
             end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
             provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.donchian(df["high"], df["low"], length), sym, src, limit)


@command("/technical/cones", providers=_PROVIDERS, summary="Realised volatility cones")
def cones(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
          trading_days: int = 252, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, "1d", provider)
    out = ta.volatility_cones(df["close"], trading_days=trading_days)
    if out.empty:
        raise EmptyDataError("Not enough history to build volatility cones for {}".format(sym))
    out.insert(0, "symbol", sym)
    return Result(out, provider=src)


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #
@command("/technical/obv", providers=_PROVIDERS, summary="On-balance volume")
def obv(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
        interval: str = "1d", limit: int = 500, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.obv(df["close"], df["volume"]).to_frame(), sym, src, limit)


@command("/technical/ad", providers=_PROVIDERS, summary="Accumulation/distribution line")
def ad(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
       interval: str = "1d", limit: int = 500, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.accumulation_distribution(df["high"], df["low"], df["close"], df["volume"])
                 .to_frame(), sym, src, limit)


@command("/technical/adosc", providers=_PROVIDERS, summary="Chaikin A/D oscillator")
def ad_oscillator(symbol: str, fast: int = 3, slow: int = 10, start_date: Optional[str] = None,
                  end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
                  provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.adosc(df["high"], df["low"], df["close"], df["volume"], fast, slow)
                 .to_frame(), sym, src, limit)


@command("/technical/cmf", providers=_PROVIDERS, summary="Chaikin money flow")
def cmf(symbol: str, length: int = 20, start_date: Optional[str] = None,
        end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
        provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.chaikin_money_flow(df["high"], df["low"], df["close"], df["volume"], length)
                 .to_frame(), sym, src, limit)


@command("/technical/mfi", providers=_PROVIDERS, summary="Money flow index")
def mfi(symbol: str, length: int = 14, start_date: Optional[str] = None,
        end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
        provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.money_flow_index(df["high"], df["low"], df["close"], df["volume"], length)
                 .to_frame(), sym, src, limit)


@command("/technical/vwap", providers=_PROVIDERS, summary="Volume-weighted average price")
def vwap(symbol: str, anchor: Optional[str] = None, start_date: Optional[str] = None,
         end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
         provider: Optional[str] = None) -> Result:
    """``anchor``: ``D``, ``W`` or ``M`` to reset the accumulation each period."""
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.vwap(df["high"], df["low"], df["close"], df["volume"], anchor).to_frame(),
                 sym, src, limit)


# --------------------------------------------------------------------------- #
# Trend & structure
# --------------------------------------------------------------------------- #
@command("/technical/adx", providers=_PROVIDERS, summary="Average directional index with DI+/DI-")
def adx(symbol: str, length: int = 14, start_date: Optional[str] = None,
        end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
        provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.adx(df["high"], df["low"], df["close"], length), sym, src, limit)


@command("/technical/aroon", providers=_PROVIDERS, summary="Aroon up/down and oscillator")
def aroon(symbol: str, length: int = 25, start_date: Optional[str] = None,
          end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
          provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.aroon(df["high"], df["low"], length), sym, src, limit)


@command("/technical/psar", providers=_PROVIDERS, summary="Parabolic SAR")
def psar(symbol: str, step: float = 0.02, maximum: float = 0.2, start_date: Optional[str] = None,
         end_date: Optional[str] = None, interval: str = "1d", limit: int = 500,
         provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.parabolic_sar(df["high"], df["low"], step, maximum).to_frame(),
                 sym, src, limit)


@command("/technical/supertrend", providers=_PROVIDERS, summary="Supertrend line and direction")
def supertrend(symbol: str, length: int = 10, multiplier: float = 3.0,
               start_date: Optional[str] = None, end_date: Optional[str] = None,
               interval: str = "1d", limit: int = 500, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.supertrend(df["high"], df["low"], df["close"], length, multiplier),
                 sym, src, limit)


@command("/technical/ichimoku", providers=_PROVIDERS, summary="Ichimoku cloud")
def ichimoku(symbol: str, conversion: int = 9, base: int = 26, span_b: int = 52,
             start_date: Optional[str] = None, end_date: Optional[str] = None,
             interval: str = "1d", limit: int = 500, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    return _emit(df, ta.ichimoku(df["high"], df["low"], df["close"], conversion, base, span_b),
                 sym, src, limit)


@command("/technical/fib", providers=_PROVIDERS, summary="Fibonacci retracement and extension levels")
def fib(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
        provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, "1d", provider)
    out = ta.fibonacci_levels(df["high"], df["low"])
    out.insert(0, "symbol", sym)
    return Result(out, provider=src,
                  extra={"high": float(df["high"].max()), "low": float(df["low"].min())})


@command("/technical/pivots", providers=_PROVIDERS, summary="Classic pivot support/resistance levels")
def pivots(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
           interval: str = "1d", provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, interval, provider)
    out = ta.pivot_points(df["high"], df["low"], df["close"])
    out.insert(0, "symbol", sym)
    return Result(out, provider=src)


# --------------------------------------------------------------------------- #
# Statistical
# --------------------------------------------------------------------------- #
@command("/technical/hurst", providers=_PROVIDERS,
         summary="Hurst exponent — trending vs mean-reverting")
def hurst(symbol: str, min_lag: int = 2, max_lag: int = 100, start_date: Optional[str] = None,
          end_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, "1d", provider)
    value = ta.hurst_exponent(df["close"], min_lag, max_lag)
    regime = "trending" if value > 0.55 else ("mean-reverting" if value < 0.45 else "random walk")
    return Result({"symbol": sym, "hurst_exponent": value, "regime": regime,
                   "observations": int(len(df))}, provider=src)


@command("/technical/clenow", providers=_PROVIDERS,
         summary="Clenow momentum: annualised regression slope x R^2")
def clenow(symbol: str, length: int = 90, start_date: Optional[str] = None,
           end_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    rows, warnings = [], []
    for sym in [s.strip() for s in symbol.split(",") if s.strip()]:
        try:
            df, resolved, src = _prices(sym, start_date, end_date, "1d", provider)
        except Exception as exc:  # noqa: BLE001
            warnings.append("{}: {}".format(sym, exc))
            continue
        rows.append(dict(symbol=resolved, **ta.clenow_momentum(df["close"], length)))
    if not rows:
        raise EmptyDataError("No Clenow momentum computed. {}".format("; ".join(warnings)))
    rows.sort(key=lambda r: -(r.get("score") or float("-inf")))
    return Result(rows, provider=resolve_provider(provider, _PROVIDERS), warnings=warnings)


@command("/technical/relative_strength", providers=_PROVIDERS,
         summary="Price ratio and relative return against a benchmark")
def relative_strength(symbol: str, benchmark: str = "SPY", length: int = 63,
                      start_date: Optional[str] = None, end_date: Optional[str] = None,
                      limit: int = 500, provider: Optional[str] = None) -> Result:
    df, sym, src = _prices(symbol, start_date, end_date, "1d", provider)
    bench, bench_sym, _ = _prices(benchmark, start_date, end_date, "1d", provider)
    out = ta.relative_strength(df["close"], bench["close"], length)
    out.insert(0, "benchmark", bench_sym)
    out.insert(0, "symbol", sym)
    return Result(out.tail(limit), provider=src, index_name="date")
