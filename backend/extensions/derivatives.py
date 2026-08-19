"""Derivatives menu: options chains, greeks, a pricer, IV surface, futures."""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, one_symbol
from ..providers import markets, yahoo
from ..valuation import blackscholes as bs

MONTH_CODES = "FGHJKMNQUVXZ"

# Futures roots -> (Yahoo exchange suffix, description)
FUTURES_ROOTS: Dict[str, "tuple"] = {
    "CL": ("NYM", "WTI Crude Oil"), "BZ": ("NYM", "Brent Crude"), "NG": ("NYM", "Natural Gas"),
    "RB": ("NYM", "RBOB Gasoline"), "HO": ("NYM", "Heating Oil"),
    "GC": ("CMX", "Gold"), "SI": ("CMX", "Silver"), "HG": ("CMX", "Copper"),
    "PL": ("NYM", "Platinum"), "PA": ("NYM", "Palladium"),
    "ZC": ("CBT", "Corn"), "ZS": ("CBT", "Soybeans"), "ZW": ("CBT", "Wheat"),
    "ZM": ("CBT", "Soybean Meal"), "ZL": ("CBT", "Soybean Oil"),
    "LE": ("CME", "Live Cattle"), "HE": ("CME", "Lean Hogs"),
    "ES": ("CME", "E-mini S&P 500"), "NQ": ("CME", "E-mini Nasdaq 100"),
    "YM": ("CBT", "E-mini Dow"), "RTY": ("CME", "E-mini Russell 2000"),
    "ZN": ("CBT", "10-Year T-Note"), "ZB": ("CBT", "30-Year T-Bond"),
    "ZF": ("CBT", "5-Year T-Note"), "ZT": ("CBT", "2-Year T-Note"),
    "6E": ("CME", "Euro FX"), "6J": ("CME", "Japanese Yen"), "6B": ("CME", "British Pound"),
}


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
@command("/derivatives/options/expirations", providers=("yahoo",),
         summary="Listed expiration dates for a symbol")
def option_expirations(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    sym = one_symbol(symbol)
    today = pd.Timestamp(date.today())
    rows = [
        {"symbol": sym, "expiration": e,
         "days_to_expiry": int((pd.Timestamp(e) - today).days)}
        for e in yahoo.option_expirations(sym)
    ]
    return Result(rows, provider=src)


@command("/derivatives/options/chains", providers=("yahoo", "cboe"),
         summary="Full options chain with greeks where published")
def option_chains(symbol: str, expiration: Optional[str] = None, option_type: Optional[str] = None,
                  provider: Optional[str] = None) -> Result:
    """Yahoo returns one expiry at a time; Cboe returns every listed expiry at once."""
    src = resolve_provider(provider, ("yahoo", "cboe"))
    sym = one_symbol(symbol)
    if src == "cboe":
        df = markets.cboe_option_chain(sym)
        if expiration:
            df = df[df["expiration"] == pd.Timestamp(expiration)]
    else:
        df = yahoo.option_chain(sym, expiration)
    if option_type:
        df = df[df["option_type"] == option_type.lower().rstrip("s")]
    if df.empty:
        raise EmptyDataError("No contracts match that filter for {}".format(sym))
    return Result(df.reset_index(drop=True), provider=src)


@command("/derivatives/options/unusual", providers=("yahoo", "cboe"),
         summary="Contracts trading far above their open interest")
def options_unusual(symbol: str, expiration: Optional[str] = None, min_volume: int = 100,
                    limit: int = 50, provider: Optional[str] = None) -> Result:
    """Ranks contracts by volume / open-interest, the classic unusual-activity screen."""
    src = resolve_provider(provider, ("yahoo", "cboe"))
    chain = option_chains(symbol, expiration, provider=src).data
    df = pd.DataFrame(chain) if not isinstance(chain, pd.DataFrame) else chain
    for col in ("volume", "open_interest"):
        if col not in df.columns:
            raise EmptyDataError("Provider {} does not publish {}".format(src, col))
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df["volume"].fillna(0) >= min_volume].copy()
    if df.empty:
        raise EmptyDataError("No contracts traded at least {} times".format(min_volume))
    df["volume_oi_ratio"] = (df["volume"] / df["open_interest"].replace(0, np.nan)).round(3)
    keep = [c for c in ("underlying_symbol", "expiration", "strike", "option_type", "contract_symbol",
                        "last_price", "bid", "ask", "volume", "open_interest", "volume_oi_ratio",
                        "implied_volatility") if c in df.columns]
    out = df[keep].sort_values("volume_oi_ratio", ascending=False).head(limit)
    return Result(out.reset_index(drop=True), provider=src)


@command("/derivatives/options/surface", providers=("yahoo", "cboe"),
         summary="Implied-volatility surface (strike x expiry)")
def options_surface(symbol: str, option_type: str = "call", max_expirations: int = 6,
                    provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo", "cboe"))
    sym = one_symbol(symbol)
    if src == "cboe":
        df = markets.cboe_option_chain(sym)
    else:
        frames = []
        for expiry in yahoo.option_expirations(sym)[:max_expirations]:
            try:
                frames.append(yahoo.option_chain(sym, expiry))
            except Exception:  # noqa: BLE001 - illiquid expiries drop out
                continue
        if not frames:
            raise EmptyDataError("No option chains available for {}".format(sym))
        df = pd.concat(frames, ignore_index=True)
    df = df[df["option_type"] == option_type.lower().rstrip("s")]
    if "implied_volatility" not in df.columns or df.empty:
        raise EmptyDataError("No implied volatilities published for {}".format(sym))
    df["expiration"] = pd.to_datetime(df["expiration"], errors="coerce")
    today = pd.Timestamp(date.today())
    df["days_to_expiry"] = (df["expiration"] - today).dt.days
    keep = ["expiration", "days_to_expiry", "strike", "implied_volatility", "volume", "open_interest"]
    out = df[[c for c in keep if c in df.columns]].dropna(subset=["implied_volatility"])
    out = out[out["implied_volatility"] > 0]
    if out.empty:
        raise EmptyDataError("Implied volatility column was empty for {}".format(sym))
    return Result(out.sort_values(["expiration", "strike"]).reset_index(drop=True), provider=src,
                  extra={"symbol": sym, "option_type": option_type})


@command("/derivatives/options/snapshots", providers=("yahoo",),
         summary="Put/call ratios and chain-level totals")
def options_snapshots(symbol: str, expiration: Optional[str] = None,
                      provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    sym = one_symbol(symbol)
    df = yahoo.option_chain(sym, expiration)
    grouped = df.groupby("option_type").agg(
        contracts=("strike", "count"),
        volume=("volume", "sum"),
        open_interest=("open_interest", "sum"),
        avg_iv=("implied_volatility", "mean"),
    )
    calls = grouped.loc["call"] if "call" in grouped.index else None
    puts = grouped.loc["put"] if "put" in grouped.index else None
    summary = {
        "symbol": sym,
        "expiration": df["expiration"].iloc[0] if "expiration" in df.columns else expiration,
        "call_volume": float(calls["volume"]) if calls is not None else None,
        "put_volume": float(puts["volume"]) if puts is not None else None,
        "call_open_interest": float(calls["open_interest"]) if calls is not None else None,
        "put_open_interest": float(puts["open_interest"]) if puts is not None else None,
        "put_call_volume_ratio": (float(puts["volume"]) / float(calls["volume"]))
        if calls is not None and puts is not None and calls["volume"] else None,
        "put_call_oi_ratio": (float(puts["open_interest"]) / float(calls["open_interest"]))
        if calls is not None and puts is not None and calls["open_interest"] else None,
        "avg_call_iv": float(calls["avg_iv"]) if calls is not None else None,
        "avg_put_iv": float(puts["avg_iv"]) if puts is not None else None,
    }
    return Result(summary, provider=src)


# --------------------------------------------------------------------------- #
# Greeks and the pricer
# --------------------------------------------------------------------------- #
DEFAULT_RATE = 0.04  # used, and flagged in a warning, only when the curve is down


def _risk_free(t_years: float, warnings: List[str]) -> float:
    """The Treasury par yield at this horizon, linearly interpolated, as a decimal."""
    try:
        from ..providers import treasury

        curve = treasury.yield_curve()
        return float(np.interp(
            max(t_years, 1e-6), curve["maturity_years"], curve["rate"])) / 100.0
    except Exception as exc:  # noqa: BLE001 - the pricer should not die with the curve
        warnings.append(
            "Treasury curve unavailable ({}); using a flat {:.0%} risk-free rate.".format(
                exc, DEFAULT_RATE))
        return DEFAULT_RATE


def _spot_and_yield(sym: str, warnings: List[str]) -> "tuple":
    q = yahoo.quote(sym)
    spot = q.get("last_price")
    if spot is None:
        raise EmptyDataError("No spot price for {}".format(sym))
    dy = q.get("dividend_yield")
    # yfinance publishes the yield in percent units (AAPL 0.35 means 0.35 %,
    # KO 2.4 means 2.4 %); ancient builds used fractions. Always divide: on a
    # modern build that is exact, on an ancient one it understates a small
    # input by 100x — harmless next to reading AAPL's 0.35 as a 35 % yield,
    # which would poison every put. Clamped because nothing listed pays 25 %.
    q_div = 0.0 if dy is None else min(max(float(dy) / 100.0, 0.0), 0.25)
    return float(spot), q_div


def _years_to_expiry(expiry: pd.Timestamp) -> float:
    """ACT/365 years from now until the 16:00 New York close on ``expiry``.

    Counting whole days would make every same-day expiry worthless at
    midnight — a 0DTE contract still has a trading session of life, and its
    greeks are exactly what a 0DTE trader is looking at.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    close = datetime(expiry.year, expiry.month, expiry.day, 16, 0, tzinfo=ny)
    seconds = (close - datetime.now(tz=ny)).total_seconds()
    return max(seconds, 0.0) / (365.0 * 24 * 3600)


@command("/derivatives/options/greeks", providers=("yahoo",),
         summary="Options chain with Black-Scholes greeks computed per contract",
         examples=("symbol=AAPL", "symbol=SPY&iv_source=solved&option_type=put"))
def option_greeks(symbol: str, expiration: Optional[str] = None, option_type: Optional[str] = None,
                  iv_source: str = "provider", provider: Optional[str] = None) -> Result:
    """Delta, gamma, theta, vega and rho for every contract in one expiry.

    Inputs are assembled from free sources: spot and dividend yield from
    Yahoo's quote, the risk-free rate read off the Treasury par curve at the
    expiry's horizon. ``iv_source`` picks the volatility the greeks are
    computed at: ``provider`` (default) uses Yahoo's published implied vol;
    ``solved`` re-derives it from the bid/ask mid (falling back to the last
    price) with this module's own solver — slower, but consistent with the
    pricer, and honest about quotes with no BS vol (``iv`` comes back null).
    Greeks are per share; a contract is 100. Theta is per calendar day; vega
    and rho are per point (1 % move in vol / rates). European-exercise model
    on American contracts: see the doc for the known bias.
    """
    src = resolve_provider(provider, ("yahoo",))
    if iv_source not in ("provider", "solved"):
        raise ValueError("iv_source must be 'provider' or 'solved'")
    sym = one_symbol(symbol)
    warnings: List[str] = []
    df = yahoo.option_chain(sym, expiration)
    if option_type:
        df = df[df["option_type"] == option_type.lower().rstrip("s")]
    if df.empty:
        raise EmptyDataError("No contracts match that filter for {}".format(sym))
    df = df.copy()
    spot, q_div = _spot_and_yield(sym, warnings)
    expiry = pd.Timestamp(df["expiration"].iloc[0])
    t_years = _years_to_expiry(expiry)
    r = _risk_free(t_years, warnings)

    for col in ("bid", "ask", "last_price", "implied_volatility"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    bid = df.get("bid")
    ask = df.get("ask")
    mid = None
    if bid is not None and ask is not None:
        mid = np.where((bid > 0) & (ask > 0), (bid + ask) / 2.0, np.nan)
        mid = np.where(np.isnan(mid), df.get("last_price", pd.Series(np.nan, index=df.index)), mid)
    else:
        mid = df.get("last_price", pd.Series(np.nan, index=df.index)).to_numpy()
    df["mid"] = mid

    strikes = pd.to_numeric(df["strike"], errors="coerce").to_numpy(dtype=float)
    types = df["option_type"].to_numpy()
    if iv_source == "solved":
        iv = bs.implied_vol(df["mid"].to_numpy(dtype=float), spot, strikes, t_years,
                            r=r, q=q_div, option_type=types)
        solved = int(np.isfinite(iv).sum())
        warnings.append(
            "Implied vol solved from the quote mid for {} of {} contracts; the rest "
            "have no Black-Scholes vol at their quoted price.".format(solved, len(df)))
    else:
        iv = df.get("implied_volatility", pd.Series(np.nan, index=df.index)).to_numpy(dtype=float)
        iv = np.where(iv > 0, iv, np.nan)
    df["iv"] = iv

    greeks = bs.bs_greeks(spot, strikes, t_years, np.nan_to_num(iv, nan=0.0),
                          r=r, q=q_div, option_type=types)
    have_iv = np.isfinite(iv)
    df["bs_price"] = np.where(have_iv, greeks["price"], np.nan)
    for g in ("delta", "gamma", "theta", "vega", "rho"):
        df[g] = np.where(have_iv, greeks[g], np.nan)

    keep = [c for c in ("underlying_symbol", "expiration", "strike", "option_type",
                        "contract_symbol", "last_price", "bid", "ask", "mid", "volume",
                        "open_interest", "iv", "bs_price", "delta", "gamma", "theta",
                        "vega", "rho", "in_the_money") if c in df.columns]
    out = df[keep].sort_values(["option_type", "strike"]).reset_index(drop=True)
    return Result(out, provider=src, warnings=warnings, extra={
        "symbol": sym, "spot": spot, "expiration": str(expiry.date()),
        "days_to_expiry": round(t_years * 365, 2), "risk_free_rate": round(r, 6),
        "dividend_yield": round(q_div, 6), "iv_source": iv_source,
        "model": "Black-Scholes-Merton, European exercise, ACT/365",
    })


@command("/derivatives/options/pricer", providers=("yahoo",),
         summary="Black-Scholes calculator: price and greeks, or implied vol from a price",
         examples=("s=100&k=105&dte=30&sigma=0.25",
                   "symbol=AAPL&k=320&dte=45&price=12.50&option_type=call"))
def option_pricer(k: float, s: Optional[float] = None, symbol: Optional[str] = None,
                  dte: float = 30.0, sigma: Optional[float] = None,
                  price: Optional[float] = None, option_type: Optional[str] = None,
                  r: Optional[float] = None, q: Optional[float] = None,
                  provider: Optional[str] = None) -> Result:
    """A standalone Black-Scholes-Merton calculator.

    Give ``sigma`` to price, or ``price`` to solve the implied volatility
    (``option_type`` required in that case) — exactly one of the two. Spot
    comes from ``s`` or live from ``symbol``, which also fills the dividend
    yield; the risk-free rate defaults to the Treasury par yield at the
    ``dte`` horizon. ``sigma``, ``r`` and ``q`` are decimals per year
    (0.25 = 25 %); ``dte`` is calendar days. Without ``option_type`` both
    sides are returned, plus a put-call-parity check that should sit at ~0.
    """
    src = resolve_provider(provider, ("yahoo",))
    if (sigma is None) == (price is None):
        raise ValueError("Give exactly one of sigma= (to price) or price= (to solve the vol)")
    if price is not None and not option_type:
        raise ValueError("Solving implied vol needs option_type=call or put")
    warnings: List[str] = []
    q_div = q
    if s is None:
        if not symbol:
            raise ValueError("Give a spot price s= or a symbol= to fetch one")
        s, fetched_q = _spot_and_yield(one_symbol(symbol), warnings)
        if q_div is None:
            q_div = fetched_q
    if q_div is None:
        q_div = 0.0
    t_years = max(float(dte), 0.0) / 365.0
    if r is None:
        r = _risk_free(t_years, warnings)

    if price is not None:
        solved = float(bs.implied_vol(price, s, k, t_years, r=r, q=q_div,
                                      option_type=option_type)[0])
        if not np.isfinite(solved):
            raise EmptyDataError(
                "No Black-Scholes volatility reproduces {:.4f} for that {} — the price "
                "sits outside the arbitrage bounds at these inputs.".format(price, option_type))
        sigma = solved
        warnings.append("Implied volatility solved from price={:.4f}.".format(price))

    sides = [option_type.lower().rstrip("s")] if option_type else ["call", "put"]
    rows, raw_prices = [], []
    for side in sides:
        g = bs.bs_greeks(s, k, t_years, sigma, r=r, q=q_div, option_type=side)
        raw_prices.append(float(g["price"][0]))
        rows.append({"option_type": side,
                     **{name: round(float(vals[0]), 6) for name, vals in g.items()}})
    extra = {
        "spot": round(float(s), 6), "strike": float(k), "dte": float(dte),
        "sigma": round(float(sigma), 6), "risk_free_rate": round(float(r), 6),
        "dividend_yield": round(float(q_div), 6), "per_contract_multiplier": 100,
        "model": "Black-Scholes-Merton, European exercise, ACT/365",
        "units": {"theta": "per calendar day", "vega": "per vol point",
                  "rho": "per rate point"},
    }
    if len(rows) == 2:
        # Checked on the unrounded prices: the display rounding above would
        # otherwise leak into a number whose whole point is to sit at zero.
        parity_gap = (raw_prices[0] - raw_prices[1]) - (
            s * np.exp(-q_div * t_years) - k * np.exp(-r * t_years))
        extra["put_call_parity_gap"] = round(float(parity_gap), 8)
    return Result(rows, provider=src, warnings=warnings, extra=extra)


# --------------------------------------------------------------------------- #
# Futures
# --------------------------------------------------------------------------- #
@command("/derivatives/futures/roots", providers=("yahoo",), summary="Supported futures roots")
def futures_roots() -> Result:
    return Result(
        [{"root": k, "exchange": v[0], "description": v[1], "continuous_symbol": k + "=F"}
         for k, v in sorted(FUTURES_ROOTS.items())],
        provider="yahoo",
    )


@command("/derivatives/futures/historical", providers=("yahoo",),
         summary="Continuous futures price history")
def futures_historical(symbol: str = "CL=F", start_date: Optional[str] = None,
                       end_date: Optional[str] = None, interval: str = "1d",
                       provider: Optional[str] = None) -> Result:
    """Pass a Yahoo continuous contract (``CL=F``) or a dated one (``CLZ26.NYM``)."""
    src = resolve_provider(provider, ("yahoo",))
    start, end = date_window(start_date, end_date)
    sym = one_symbol(symbol)
    if sym in FUTURES_ROOTS:
        sym = sym + "=F"
    return Result(yahoo.history(sym, str(start), str(end), interval=interval),
                  provider=src, index_name="date")


@command("/derivatives/futures/curve", providers=("yahoo",),
         summary="Term structure across listed contract months")
def futures_curve(root: str = "CL", months: int = 12, provider: Optional[str] = None) -> Result:
    """Builds the forward curve by quoting each listed monthly contract."""
    src = resolve_provider(provider, ("yahoo",))
    root = root.upper().replace("=F", "").strip()
    if root not in FUTURES_ROOTS:
        raise ValueError(
            "Unknown futures root {!r}. See /derivatives/futures/roots.".format(root)
        )
    suffix, description = FUTURES_ROOTS[root]
    today = date.today()
    rows, warnings = [], []
    for i in range(months):
        total = (today.year * 12 + today.month - 1) + i
        year, month = divmod(total, 12)
        month += 1
        ticker = "{}{}{:02d}.{}".format(root, MONTH_CODES[month - 1], year % 100, suffix)
        try:
            quote = yahoo.quote(ticker)
        except Exception as exc:  # noqa: BLE001 - not every month is listed
            warnings.append("{}: {}".format(ticker, exc))
            continue
        if quote.get("last_price") is None:
            continue
        rows.append(
            {
                "root": root, "description": description, "symbol": ticker,
                "expiration_month": "{}-{:02d}".format(year, month),
                "price": quote.get("last_price"), "volume": quote.get("volume"),
                "months_out": i,
            }
        )
    if not rows:
        raise EmptyDataError(
            "No listed {} contracts returned a quote. {}".format(root, "; ".join(warnings[:3]))
        )
    front = rows[0]["price"]
    for r in rows:
        r["spread_to_front"] = round(r["price"] - front, 6) if front else None
    shape = "contango" if len(rows) > 1 and rows[-1]["price"] > rows[0]["price"] else "backwardation"
    return Result(rows, provider=src, warnings=warnings, extra={"curve_shape": shape})
