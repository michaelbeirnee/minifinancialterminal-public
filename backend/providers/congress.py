"""Congressional trading disclosures — the STOCK Act read from its own source.

The STOCK Act makes a member of Congress file a Periodic Transaction Report
within 45 days of a trade over $1,000, naming the security and a bracket for
the size. That report is the whole feed. It says who traded, what, which way
and roughly how much, and it is published by the chamber itself — no vendor
sits in between, and there is nothing to pay for.

**Senate only, and that is a coverage statement, not an oversight.** The Senate
publishes electronically-filed PTRs as HTML at
``efdsearch.senate.gov``, which is a table this module can read. The House
publishes its PTRs as PDFs at ``disclosures-clerk.house.gov``; extracting them
would mean adding a PDF dependency to a project that has none, and OCR for the
paper filings on top. The House index (``{year}FD.ZIP``) names who filed and
when but never what they bought, which cannot support a per-symbol signal. So
this covers 100 of 535 members. :mod:`backend.extensions.congress` says so on
every result rather than letting thin coverage read as quiet coverage.

Three things the numbers here are not:

* **Not sized.** Amounts are brackets ("$50,001 - $100,000"), so the low and
  high are both reported and neither is the trade.
* **Not timely.** Up to 45 days may separate the trade from its disclosure,
  and the filing date is the first date the market could know. Grading anchors
  on the filing date for that reason; anchoring on the transaction date would
  score a signal nobody could have acted on.
* **Not necessarily the member's decision.** ``owner`` distinguishes self from
  spouse and dependent child, and a filer using a managed account discloses
  trades they did not direct. The column is kept so a caller can filter on it
  rather than being told an aggregate that hides it.

The search endpoint requires agreeing to a prohibition notice first, which
sets a session cookie; :func:`_authorise` does that once per process.
"""
from __future__ import annotations

import html as html_lib
import re
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..core.caching import TTL_DAILY, TTL_REFERENCE, cached
from ..core.errors import EmptyDataError, ProviderError
from ..core.http import get_client, get_text

NAME = "senate"

BASE = "https://efdsearch.senate.gov"
HOME = BASE + "/search/home/"
SEARCH = BASE + "/search/report/data/"

#: The Senate's report-type code for a Periodic Transaction Report. Annual
#: reports and blind-trust filings share the search endpoint and are not
#: transactions, so the funnel asks for this one only.
PTR_REPORT_TYPE = "[11]"

_CSRF = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href="([^"]+)"')

#: A ticker cell holds "--" for anything without one (municipal bonds, funds
#: held by name, real estate). Treating that as a symbol would put "--" on a
#: chart, so it is dropped to None along with the other placeholders EFD uses.
_NO_TICKER = {"", "--", "-", "n/a", "na", "none"}

#: EFD renders the ticker cell twice over: the raw disclosed value, which is
#: "--" whenever the filer left it blank, and beside it a resolved quote link.
#: Flattening the cell to text yields "-- AMCR", which is neither, so the link
#: is read first — it is the value EFD itself resolved.
_QUOTE_LINK = re.compile(r"/quote/([A-Za-z0-9.\-]+)")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

#: "$50,001 - $100,000" and "Over $50,000,000" are both bracket notations.
_AMOUNT = re.compile(r"\$\s*([\d,]+)(?:\s*(?:-|–|to)\s*\$\s*([\d,]+))?")

#: The ``owner`` values naming an account the member is a party to. The others
#: EFD emits — Spouse, Child — are disclosed because the STOCK Act covers the
#: household, not because the member placed the trade. This lives here, with
#: the column it describes, so the gate and the triage card cannot drift into
#: two different definitions of whose trade it was.
MEMBER_ACCOUNTS = frozenset({"self", "joint"})


def is_member_account(owner: Any) -> bool:
    return str(owner or "").strip().lower() in MEMBER_ACCOUNTS

#: Bumped whenever a parsing rule above changes. It is baked into the cache
#: keys, so a fix retires every parse made under the old rules instead of
#: leaving them to be served from disk forever.
_PARSER_VERSION = 2

_session_lock = threading.Lock()
_session_token: Optional[str] = None


def _headers() -> Dict[str, str]:
    return {"Referer": HOME, "X-Requested-With": "XMLHttpRequest"}


def _authorise(force: bool = False) -> str:
    """Accept the prohibition notice once and return the CSRF token.

    The search endpoint 403s until the caller has agreed to the notice, which
    is a form POST that sets a session cookie. The cookie lives on the shared
    HTTP client, so this runs once per process rather than once per query.
    """
    global _session_token
    with _session_lock:
        if _session_token and not force:
            return _session_token
        client = get_client()
        try:
            home = client.get(HOME)
            home.raise_for_status()
            match = _CSRF.search(home.text)
            if match is None:
                raise ProviderError("Senate EFD did not serve a CSRF token")
            token = match.group(1)
            agreed = client.post(
                HOME,
                data={"csrfmiddlewaretoken": token, "prohibition_agreement": "1"},
                headers={"Referer": HOME},
            )
            if agreed.status_code >= 400:
                raise ProviderError(
                    "Senate EFD refused the prohibition agreement (HTTP {})".format(
                        agreed.status_code)
                )
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - httpx errors are the common case
            raise ProviderError("Senate EFD unreachable: {}".format(exc)) from exc
        _session_token = token
        return token


def _text(fragment: str) -> str:
    """Cell HTML to flat text, entities resolved and whitespace collapsed."""
    return re.sub(r"\s+", " ", html_lib.unescape(_TAG.sub(" ", fragment))).strip()


def parse_amount(raw: str) -> Tuple[Optional[float], Optional[float]]:
    """``"$50,001 - $100,000"`` -> ``(50001.0, 100000.0)``.

    An open-ended top bracket ("Over $50,000,000") has no high, which is
    reported as ``None`` rather than as its own floor: the point of the column
    is the range, and inventing a ceiling would be inventing data.
    """
    match = _AMOUNT.search(raw or "")
    if match is None:
        return None, None
    low = float(match.group(1).replace(",", ""))
    high = float(match.group(2).replace(",", "")) if match.group(2) else None
    return low, high


def _ticker(fragment: str) -> Optional[str]:
    """The symbol from a ticker cell, or ``None`` when the filer named none.

    Anything that is not shaped like a ticker is dropped rather than passed on:
    a symbol column that sometimes holds an asset description is worse than one
    that is sometimes empty, because only the second kind is obvious.
    """
    link = _QUOTE_LINK.search(fragment or "")
    candidate = (link.group(1) if link else _text(fragment)).upper()
    if candidate.lower() in _NO_TICKER:
        return None
    return candidate if _TICKER.match(candidate) else None


def _side(raw: str) -> str:
    """Normalise EFD's transaction wording to buy / sell / exchange."""
    lowered = (raw or "").lower()
    if "purchase" in lowered:
        return "buy"
    if "sale" in lowered or "sold" in lowered:
        return "sell"
    if "exchange" in lowered:
        return "exchange"
    return lowered.strip() or "unknown"


def transactions_in(document: str) -> List[Dict[str, Any]]:
    """Parse one PTR document's transaction table. Offline, so it is testable.

    The table is positional — ``#``, transaction date, owner, ticker, asset,
    asset type, transaction type, amount, comment — and a row that does not
    carry at least the date, type and amount columns is not a transaction row
    (the document also contains header and layout tables).
    """
    rows: List[Dict[str, Any]] = []
    for chunk in _ROW.findall(document):
        raw_cells = _CELL.findall(chunk)
        cells = [_text(c) for c in raw_cells]
        if len(cells) < 8:
            continue
        traded_on = _parse_date(cells[1])
        if traded_on is None:  # header row, or a layout table that looks close
            continue
        low, high = parse_amount(cells[7])
        rows.append({
            "transaction_date": traded_on.isoformat(),
            "owner": cells[2] or None,
            "symbol": _ticker(raw_cells[3]),
            "asset": cells[4] or None,
            "asset_type": cells[5] or None,
            "side": _side(cells[6]),
            "amount_low": low,
            "amount_high": high,
            "comment": cells[8] if len(cells) > 8 and cells[8] not in {"--", ""} else None,
        })
    return rows


def _parse_date(raw: str) -> Optional[date]:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


@cached("congress.filings.v{}".format(_PARSER_VERSION), ttl=TTL_DAILY)
def filings(days: int = 90) -> pd.DataFrame:
    """Every PTR filed in the last ``days`` days, newest filing first.

    One row per *report*, not per trade: the transactions live in the document
    each row links to. Amendments are included and marked, because an amended
    report is how a late or corrected disclosure reaches the public.
    """
    token = _authorise()
    start = (date.today() - timedelta(days=max(1, int(days)))).strftime("%m/%d/%Y")
    client = get_client()

    collected: List[Dict[str, Any]] = []
    offset, page_size = 0, 100
    while True:
        payload = {
            "csrfmiddlewaretoken": token,
            "draw": "1",
            "start": str(offset),
            "length": str(page_size),
            "report_types": PTR_REPORT_TYPE,
            "filer_types": "[]",
            "submitted_start_date": "{} 00:00:00".format(start),
            "submitted_end_date": "",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
        }
        try:
            response = client.post(SEARCH, data=payload, headers=_headers())
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("Senate EFD search failed: {}".format(exc)) from exc
        if response.status_code == 403 and offset == 0:
            token = _authorise(force=True)  # session expired mid-process
            payload["csrfmiddlewaretoken"] = token
            response = client.post(SEARCH, data=payload, headers=_headers())
        if response.status_code >= 400:
            raise ProviderError("Senate EFD search returned HTTP {}".format(
                response.status_code))

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("Senate EFD search returned non-JSON") from exc

        batch = body.get("data") or []
        for row in batch:
            if len(row) < 5:
                continue
            link = _HREF.search(row[3] or "")
            filed = _parse_date(_text(row[4]))
            collected.append({
                "member": _text(row[2]),
                "first_name": _text(row[0]),
                "last_name": _text(row[1]),
                "report": _text(row[3]),
                "amended": "amendment" in _text(row[3]).lower(),
                "filing_date": filed.isoformat() if filed else None,
                "url": BASE + link.group(1) if link else None,
            })
        offset += page_size
        if len(batch) < page_size or offset >= int(body.get("recordsTotal") or 0):
            break

    frame = pd.DataFrame(collected)
    if frame.empty:
        raise EmptyDataError("No Senate PTRs filed in the last {} days".format(days))
    return frame.dropna(subset=["url", "filing_date"]).sort_values(
        "filing_date", ascending=False).reset_index(drop=True)


@cached("congress.report.v{}".format(_PARSER_VERSION), ttl=TTL_REFERENCE)
def report_transactions(url: str) -> List[Dict[str, Any]]:
    """One PTR's transactions. Cached hard — a filed report never changes."""
    _authorise()
    try:
        document = get_text(url, headers={"Referer": HOME}, ttl=TTL_REFERENCE)
    except Exception as exc:  # noqa: BLE001 - a paper filing 404s here
        raise ProviderError("Could not read {}: {}".format(url, exc)) from exc
    return transactions_in(document)


def recent(days: int = 90, symbol: Optional[str] = None,
           reports: int = 120) -> pd.DataFrame:
    """PTR transactions filed in the window, one row per disclosed trade.

    ``reports`` caps how many documents are opened, because each is its own
    request. The cap is applied to the newest filings, and the result says how
    many were read so a caller can tell a quiet fortnight from a truncated one.
    """
    index = filings(days=days)
    opened = index.head(max(1, int(reports)))

    rows: List[Dict[str, Any]] = []
    for record in opened.to_dict("records"):
        try:
            trades = report_transactions(record["url"])
        except Exception:  # noqa: BLE001 - a paper filing must not fail the sweep
            continue
        for trade in trades:
            rows.append({
                "member": record["member"],
                "filing_date": record["filing_date"],
                "amended": record["amended"],
                "filing_url": record["url"],
                **trade,
            })

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise EmptyDataError(
            "No Senate transactions parsed from the last {} days".format(days))
    if symbol:
        frame = frame[frame["symbol"] == symbol.upper().strip()]
        if frame.empty:
            raise EmptyDataError(
                "No Senate disclosures naming {} in the last {} days".format(
                    symbol.upper(), days))
    frame.attrs["reports_read"] = len(opened)
    frame.attrs["reports_available"] = len(index)
    return frame.sort_values(["filing_date", "transaction_date"],
                             ascending=False).reset_index(drop=True)
