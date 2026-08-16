"""Derivatives menu: options chains, IV surface, unusual activity, futures."""
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
