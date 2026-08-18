"""The published basket, line by line, from State Street.

Yahoo answers "what does this ETF hold" with ten rows. For a sector fund that
is not an answer: XLE's top ten *is* 85% of the fund, XLK's is 55%, and the
question a sector reader is actually asking — how concentrated is this, what
else is in it, which names moved it — cannot be asked of a truncated list.

Fund sponsors publish the whole basket themselves, daily, because they are
required to. State Street's file is one spreadsheet per ticker at a stable URL,
which makes every SPDR fund readable from one pattern — and the eleven Select
Sector SPDRs are exactly the ETFs the sectors view is built on.

What comes back is the fund's own accounting rather than a tidy list of stocks,
so three kinds of line share the file and are separated here by
:func:`_line_type`:

* **equity** — a real position, the thing a reader means by "a holding",
* **cash** — the residual USD line, which is often slightly *negative* (an
  unsettled trade), and
* **futures** and **other** — an index future standing in for cash, or a
  contingent-value right left over from a takeover, both of which carry a
  ticker no price source will recognise.

Weights are published in percentage points and converted to fractions here, so
that every weight in the platform means the same thing.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

import pandas as pd

from ..core.caching import TTL_DAILY, cached
from ..core.errors import EmptyDataError, ProviderError
from ..core.http import get_excel

HOLDINGS_URL = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/"
    "etfs/us/holdings-daily-us-en-{}.xlsx"
)

# The eleven Select Sector SPDRs, which between them partition the S&P 500.
SECTOR_FUNDS: Dict[str, str] = {
    "XLK": "Technology", "XLV": "Health Care", "XLF": "Financials", "XLE": "Energy",
    "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLI": "Industrials",
    "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate",
    "XLC": "Communication Services",
}

# A ticker that could belong to a US-listed common share. Anything else in the
# ticker column is a cash pseudo-symbol, a future or a stub from a corporate
# action, none of which can be priced or compared with the rest of the basket.
_TICKER = re.compile(r"[A-Z][A-Z\-]{0,5}")
# Non-security lines get a pseudo-identifier in place of a CUSIP: ``999`` for a
# currency balance and ``ADI`` for a listed future. Both are more reliable than
# the name, which is free text and abbreviated to fit ("XAK TECHNOLOGY SEP26").
_CURRENCY_ID, _FUTURES_ID = "999", "ADI"
_CASH_NAME = re.compile(r"\b(MONEY MARKET|CASH)\b")
_FUTURES_NAME = re.compile(r"\b(FUT|FUTURE|EMINI|E-MINI)\b")
_HEADERS = ("name", "ticker", "identifier", "sedol", "weight", "sector", "shares_held",
            "local_currency")


def _line_type(ticker: str, name: str, identifier: str) -> str:
    """Classify one line of the file: equity, cash, futures or other."""
    upper = (name or "").upper()
    if identifier.startswith(_FUTURES_ID) or _FUTURES_NAME.search(upper):
        return "futures"
    if identifier.startswith(_CURRENCY_ID) or _CASH_NAME.search(upper):
        return "cash"
    return "equity" if _TICKER.fullmatch(ticker) else "other"


def _cell(frame: pd.DataFrame, row: int, col: int) -> Optional[str]:
    try:
        value = frame.iat[row, col]
    except IndexError:
        return None
    return None if pd.isna(value) else str(value).strip()


@cached("spdr.holdings", ttl=TTL_DAILY)
def fund_holdings(symbol: str) -> pd.DataFrame:
    """Every line of one SPDR fund's published basket, heaviest first.

    Columns: ``symbol``, ``name``, ``cusip``, ``sedol``, ``weight`` (a fraction
    of the fund), ``shares_held``, ``line_type``, ``currency``. The fund's own
    name and the as-of date it published ride along in ``df.attrs``.
    """
    ticker = symbol.strip().upper()
    try:
        raw = get_excel(HOLDINGS_URL.format(ticker.lower()), ttl=TTL_DAILY,
                        read_kwargs={"header": None})
    except ProviderError as exc:
        if "HTTP 404" not in str(exc):
            raise
        raise EmptyDataError(
            "State Street publishes no holdings file for {}. This reads the sponsor's own "
            "daily basket, so it covers SPDR funds — for anything else, /etf/holdings has "
            "Yahoo's top ten.".format(ticker)
        )

    # A short preamble (fund name, ticker, as-of date) sits above the table, so
    # find the header row rather than assuming it has stayed put.
    header = next((i for i in range(min(20, len(raw)))
                   if str(raw.iat[i, 0]).strip().lower() == "name"), None)
    if header is None:
        raise ProviderError("No holdings table in State Street's file for {}".format(ticker))

    df = raw.iloc[header + 1:].copy()
    df.columns = [str(c).strip().lower().replace(" ", "_")
                  for c in raw.iloc[header].tolist()][:df.shape[1]]
    missing = [c for c in ("name", "ticker", "weight") if c not in df.columns]
    if missing:
        raise ProviderError(
            "State Street's {} file is missing the {} column(s)".format(ticker, ", ".join(missing))
        )
    df = df.rename(columns={"identifier": "cusip", "local_currency": "currency"})
    for col in _HEADERS:
        df[col] = df.get(col)

    text = lambda col: df[col].fillna("").astype(str).str.strip()  # noqa: E731
    out = pd.DataFrame(
        {
            # Share classes are written "BRK.B" here and "BRK-B" everywhere a
            # price comes from, so settle on the form the rest of the platform
            # already uses for index membership.
            "symbol": text("ticker").str.upper().str.replace(".", "-", regex=False),
            "name": text("name"),
            "cusip": text("cusip"),
            "sedol": text("sedol"),
            # "-" is how the file writes a blank, and it is also a legal value
            # in the weight column of a cash line, so coerce rather than filter.
            "weight": pd.to_numeric(df["weight"], errors="coerce") / 100.0,
            "shares_held": pd.to_numeric(df["shares_held"], errors="coerce"),
            "currency": text("currency").replace("", "USD"),
        }
    )
    out = out[(out["name"] != "") & out["weight"].notna()].reset_index(drop=True)
    if out.empty:
        raise EmptyDataError("State Street published no holdings for {}".format(ticker))
    out["line_type"] = [
        _line_type(s, n, c) for s, n, c in zip(out["symbol"], out["name"], out["cusip"])
    ]
    out.loc[out["line_type"] != "equity", "symbol"] = ""
    out = out.sort_values("weight", ascending=False, kind="mergesort").reset_index(drop=True)

    out.attrs["fund_name"] = _cell(raw, 0, 1) or ticker
    out.attrs["as_of"] = _as_of(_cell(raw, 2, 1))
    out.attrs["ticker"] = ticker
    return out


def _as_of(value: Optional[str]) -> Optional[str]:
    """``"As of 14-Aug-2026"`` -> ``"2026-08-14"``; anything unparsable is dropped."""
    if not value:
        return None
    stamp = pd.to_datetime(re.sub(r"(?i)^as\s+of\s+", "", value).strip(), errors="coerce")
    return None if pd.isna(stamp) else stamp.date().isoformat()


def holdings_meta(df: pd.DataFrame) -> Dict[str, Any]:
    """The frame's attrs as a plain dict, for a command's ``extra`` payload."""
    return {"fund_name": df.attrs.get("fund_name"), "as_of": df.attrs.get("as_of")}
