"""The daily brief: one command composing the day's most important numbers.

The point is judgment, not another data dump — each figure arrives with a
plain-English reading (is the VIX calm or stressed? is the curve inverted?
how wide is high-yield?) and the pieces roll up into a single risk-regime
call. Sections are fetched in parallel and each one degrades independently,
so a dead provider costs a warning, not the brief.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from ..core.models import Result
from ..core.registry import command
from ..providers import fred, markets, newsfeeds, treasury, yahoo

INDEXES = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"), ("^RUT", "Russell 2000")]
CROSS = [("CL=F", "WTI Crude"), ("GC=F", "Gold"), ("BTC-USD", "Bitcoin"),
         ("DX-Y.NYB", "Dollar Index"), ("EURUSD=X", "EUR/USD")]
SECTOR_ETFS = {
    "XLK": "Technology", "XLV": "Health Care", "XLF": "Financials", "XLE": "Energy",
    "XLY": "Cons Discretionary", "XLP": "Cons Staples", "XLI": "Industrials",
    "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Communication",
}


def _quotes(pairs) -> List[Dict[str, Any]]:
    out = []
    for symbol, label in pairs:
        q = yahoo.quote(symbol)
        out.append({
            "symbol": symbol, "label": label, "last": q.get("last_price"),
            "change_percent": q.get("change_percent"), "year_high": q.get("year_high"),
        })
    return out


def _rates() -> Dict[str, Any]:
    df = treasury.rates(start_date=str(pd.Timestamp.today().date() - pd.Timedelta(14, unit="D")))
    def col(name):
        series = df[name].dropna() if name in df.columns else pd.Series(dtype=float)
        if series.empty:
            return None, None
        latest = float(series.iloc[-1])
        prev = float(series.iloc[-2]) if len(series) > 1 else latest
        return latest, latest - prev
    ten, ten_chg = col("10 yr")
    two, _ = col("2 yr")
    three_mo, _ = col("3 mo")
    thirty, _ = col("30 yr")
    return {
        "as_of": str(df.index[-1].date()), "three_month": three_mo, "two_year": two,
        "ten_year": ten, "ten_year_change_bp": None if ten_chg is None else round(ten_chg * 100, 1),
        "thirty_year": thirty,
        "spread_10y_2y": None if ten is None or two is None else round(ten - two, 2),
    }


def _credit() -> Dict[str, Any]:
    df = fred.series("BAMLH0A0HYM2,BAMLC0A0CM",
                     start_date=str(pd.Timestamp.today().date() - pd.Timedelta(45, unit="D")))
    def latest(colname):
        series = df[colname].dropna() if colname in df.columns else pd.Series(dtype=float)
        return None if series.empty else float(series.iloc[-1])
    return {"hy_oas": latest("BAMLH0A0HYM2"), "ig_oas": latest("BAMLC0A0CM")}


def _sectors() -> List[Dict[str, Any]]:
    rows = []
    for etf, label in SECTOR_ETFS.items():
        try:
            q = yahoo.quote(etf)
        except Exception:  # noqa: BLE001 - one sector missing is survivable
            continue
        if q.get("change_percent") is not None:
            rows.append({"symbol": etf, "label": label, "change_percent": q["change_percent"]})
    rows.sort(key=lambda r: -r["change_percent"])
    return rows


def _movers() -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, screen in (("gainers", "day_gainers"), ("losers", "day_losers"),
                        ("active", "most_actives")):
        try:
            df = yahoo.predefined_screen(screen, 5)
        except Exception:  # noqa: BLE001
            out[key] = []
            continue
        out[key] = [
            {"symbol": r.get("symbol"), "name": str(r.get("shortName") or "")[:28],
             "price": r.get("regularMarketPrice"),
             "change_percent": (r.get("regularMarketChangePercent") or 0) / 100.0}
            for r in df.head(4).to_dict("records")
        ]
    return out


def _headlines() -> List[Dict[str, Any]]:
    df = newsfeeds.world_news("cnbc_top,marketwatch,yahoo", limit=6)
    keep = [c for c in ("date", "title", "source", "url") if c in df.columns]
    return df[keep].to_dict("records")


def _earnings() -> List[Dict[str, Any]]:
    df = markets.nasdaq_calendar("earnings")
    name_col = next((c for c in ("company_name", "name") if c in df.columns), None)
    rows = []
    for r in df.head(8).to_dict("records"):
        rows.append({"symbol": r.get("symbol"),
                     "name": str(r.get(name_col) or "")[:34] if name_col else "",
                     "date": r.get("calendar_date")})
    return [r for r in rows if r["symbol"]]


# ---- readings: the judgment layer ---------------------------------------- #
def _vix_reading(level: float) -> str:
    if level < 14:
        return "very calm — hedges are cheap"
    if level < 18:
        return "calm"
    if level < 25:
        return "elevated — markets on edge"
    if level < 32:
        return "high stress"
    return "crisis-level fear"


def _hy_reading(oas: float) -> str:
    if oas < 3.0:
        return "tight — credit sees little risk"
    if oas < 4.5:
        return "normal"
    if oas < 6.0:
        return "widening — credit is nervous"
    return "stressed"


def _curve_reading(spread: float) -> str:
    if spread < 0:
        return "INVERTED — classic recession warning"
    if spread < 0.5:
        return "flat — late-cycle shape"
    return "normal upward slope"


@command("/overview/brief", providers=("yahoo", "treasury", "fred", "rss", "nasdaq"),
         summary="Daily market brief: the numbers that matter, with readings")
def daily_brief() -> Result:
    """Composite of index levels, the 10Y and curve, credit spreads, sector
    breadth, cross-asset moves, top movers, headlines and today's earnings —
    each with a plain-English reading and an overall risk-regime call."""
    warnings: List[str] = []

    def guard(name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - a section may fail, the brief may not
            warnings.append("{}: {}".format(name, exc))
            return None

    jobs: Dict[str, Callable[[], Any]] = {
        "indexes": lambda: _quotes(INDEXES),
        "vix": lambda: yahoo.quote("^VIX"),
        "rates": _rates,
        "credit": _credit,
        "cross_asset": lambda: _quotes(CROSS),
        "sectors": _sectors,
        "movers": _movers,
        "headlines": _headlines,
        "earnings_today": _earnings,
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {name: pool.submit(guard, name, fn) for name, fn in jobs.items()}
        data = {name: future.result() for name, future in futures.items()}

    signals: List[Dict[str, Any]] = []
    score = 0

    def sig(label: str, value: str, reading: str, tone: str) -> None:
        signals.append({"label": label, "value": value, "reading": reading, "tone": tone})

    spx = next((r for r in (data["indexes"] or []) if r["symbol"] == "^GSPC"), None)
    if spx and spx["last"] is not None:
        chg = spx["change_percent"] or 0
        score += 1 if chg > 0.003 else (-1 if chg < -0.003 else 0)
        off_high = ""
        if spx.get("year_high"):
            dist = spx["last"] / spx["year_high"] - 1
            off_high = ("at 52-week high" if dist > -0.005
                        else "{:.1%} below 52-week high".format(dist))
        sig("S&P 500", "{:,.0f} ({:+.2%})".format(spx["last"], chg), off_high,
            "pos" if chg >= 0 else "neg")

    sectors = data["sectors"] or []
    if sectors:
        up = sum(1 for r in sectors if r["change_percent"] > 0)
        score += 1 if up >= 8 else (-1 if up <= 3 else 0)
        best, worst = sectors[0], sectors[-1]
        sig("Breadth", "{} of {} sectors up".format(up, len(sectors)),
            "best {} {:+.1%} · worst {} {:+.1%}".format(
                best["label"], best["change_percent"], worst["label"], worst["change_percent"]),
            "pos" if up >= 6 else "neg")

    vix = data["vix"]
    if vix and vix.get("last_price") is not None:
        level = vix["last_price"]
        score += 1 if level < 17 else (-1 if level > 25 else 0)
        sig("VIX", "{:.1f}".format(level), _vix_reading(level),
            "pos" if level < 18 else ("warn" if level < 25 else "neg"))

    rates = data["rates"]
    if rates and rates.get("ten_year") is not None:
        chg_bp = rates.get("ten_year_change_bp")
        sig("10Y Treasury", "{:.2f}%".format(rates["ten_year"]),
            "{}{} bp on the day".format("+" if (chg_bp or 0) >= 0 else "", chg_bp)
            if chg_bp is not None else "", "neutral")
        if rates.get("spread_10y_2y") is not None:
            spread = rates["spread_10y_2y"]
            sig("Curve 10Y-2Y", "{:+.2f}%".format(spread), _curve_reading(spread),
                "neg" if spread < 0 else "neutral")

    credit = data["credit"]
    if credit and credit.get("hy_oas") is not None:
        oas = credit["hy_oas"]
        score += 1 if oas < 3.5 else (-1 if oas > 5 else 0)
        sig("High-yield OAS", "{:.2f}%".format(oas), _hy_reading(oas),
            "pos" if oas < 3.5 else ("warn" if oas < 5 else "neg"))

    for row in data["cross_asset"] or []:
        if row["symbol"] in ("DX-Y.NYB", "BTC-USD") and row["last"] is not None:
            chg = row["change_percent"] or 0
            reading = ("dollar {}".format("firmer" if chg > 0 else "softer")
                       if row["symbol"] == "DX-Y.NYB"
                       else "risk-appetite proxy")
            sig(row["label"],
                "{:,.0f} ({:+.2%})".format(row["last"], chg) if row["last"] >= 1000
                else "{:.2f} ({:+.2%})".format(row["last"], chg),
                reading, "pos" if chg >= 0 else "neg")

    regime = "RISK-ON" if score >= 2 else ("RISK-OFF" if score <= -2 else "MIXED")
    regime_tone = {"RISK-ON": "pos", "RISK-OFF": "neg", "MIXED": "warn"}[regime]
    signals.insert(0, {
        "label": "Regime", "value": regime,
        "reading": "equities, breadth, VIX and credit combined", "tone": regime_tone,
    })

    payload = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "signals": signals,
        "indexes": data["indexes"] or [],
        "rates": rates or {},
        "cross_asset": data["cross_asset"] or [],
        "sectors_today": sectors,
        "movers": data["movers"] or {},
        "headlines": data["headlines"] or [],
        "earnings_today": data["earnings_today"] or [],
    }
    return Result(payload, provider="composite", warnings=warnings)
