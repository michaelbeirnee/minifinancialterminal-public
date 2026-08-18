"""SEC Form 13F data sets — every institutional holding, aggregated per issuer.

The 13F machinery elsewhere in this platform works from one filer's side
(:mod:`backend.thesis.holders`: does *this* fund hold *that* name). This module
works from the market's side: SEC publishes every 13F information table filed
in a three-month window as one structured data set — the ~10,000 filings that
land between the quarter end and the 45-day deadline, parsed to TSV — so the
whole market's reported institutional position in every CUSIP, and the change
in it since the previous quarter, is a groupby rather than ten thousand
document reads.

Three things about the source decide the shape of what is below:

* **Windows, not quarters.** A data set is the filings received in a window
  (``01mar2026-31may2026``), which is overwhelmingly one report period (the
  quarter that ended 45 days before the window closed) plus a tail of late
  filings and amendments for older ones. Positions are therefore built *per
  report period*, taking each filer's newest complete filing for that period.
* **No tickers.** An information table names the issuer in free text and by
  CUSIP. The free source that carries both CUSIP and ticker for every
  exchange-listed security is SEC's own fails-to-deliver file, so the map is
  built from a few half-months of those (:func:`cusip_symbol_map`).
* **The change is not the flow.** A filer that dropped below the reporting
  threshold, or filed late, has every position vanish; a filer that first
  crossed it has every position appear. Neither is a sale or a purchase. The
  net change here is computed only across filers present in *both* periods,
  and the shares that entered or left with a filer are reported separately.

The 400 MB information table reads in a few seconds with the right dtypes, so
the raw archive is fetched uncached and only the reduced per-filer position
table (~80 MB in memory, four columns) is kept, per period, for a month — a
closed window never changes.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..config import settings
from ..core.caching import TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import fetch, get_text

NAME = "sec"

BASE = "https://www.sec.gov"
INDEX = BASE + "/data-research/sec-markets-data/form-13f-data-sets"
FTD = BASE + "/files/data/fails-deliver-data"

#: A closed window's data set never changes; keep the reduced table a month.
TTL_WINDOW = 30 * 86_400

#: The archive stamps dates as ``31-MAR-2026``.
_DATE_FMT = "%d-%b-%Y"

#: Managers whose flows are index arithmetic rather than a view. When a name
#: enters or leaves the Russell 2000 at the June reconstitution these are who
#: buy or sell it, in size, on one day — and a screen for "large institutional
#: flow at a small cap" is a screen for exactly that unless it says so.
_PASSIVE = re.compile(
    r"\bVANGUARD\b|\bBLACKROCK\b|\bSTATE STREET\b|\bGEODE\b|\bNORTHERN TRUST\b|"
    r"SCHWAB INVESTMENT|\bRHUMBLINE\b|\bSSGA\b|MELLON INVESTMENTS|"
    r"INVESCO CAPITAL MANAGEMENT|LEGAL & GENERAL",
    re.I,
)

_WINDOW = re.compile(r"(\d{2}[a-z]{3}\d{4})-(\d{2}[a-z]{3}\d{4})_form13f\.zip", re.I)

#: Classes that are not the common stock, however the filer typed the share
#: count. Convertible notes, warrants, rights, preferreds and units have their
#: own CUSIPs and their own (usually nonexistent) trading volume; a screen for
#: flow against volume has to be about the equity.
_NOT_COMMON = re.compile(
    r"\bNOTE|\bNT\b|\bCVT\b|CONV|DEBT|\bBOND|\bBD\b|DEBENT|\bPUT\b|\bCALL\b|"
    r"WARRANT|\bWT\b|\bWTS\b|\bRIGHT|\bRT\b|\bRTS\b|\bPFD\b|PREF|\bUNIT|\bUNT\b|"
    r"\bSDCV\b|\bLP\b|\bETN\b",
    re.I,
)

#: When at least this share of a CUSIP's prior holders closed the position in
#: one quarter and the remaining shares are a rounding error, the likeliest
#: cause is not eight hundred managers selling in concert — it is the CUSIP
#: changing under them (a redomicile, a reverse split, a merger exchange) or
#: the company ceasing to trade. Amcor's Jersey CUSIP showed 605 "exits" the
#: quarter its new one appeared. Such rows are labelled, not screened.
CUSIP_TURNOVER = 0.80
MIN_HOLDERS_FOR_IDENTITY = 5


def _headers() -> Dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


# --------------------------------------------------------------------------- #
# Which data sets exist
# --------------------------------------------------------------------------- #
@cached("thirteenf.windows.v2", ttl=86_400)
def windows() -> List[Dict[str, Any]]:
    """Every published data set, newest first, with the period it mostly covers.

    A window opens on the first day of the month a quarter ends in and runs
    three months — long enough for the 45-day deadline and the amendments that
    trail it — so the period it carries is the last day of its opening month:
    ``01mar2026-31may2026`` is the 31-Mar-2026 reports.
    """
    html = get_text(INDEX, headers=_headers(), ttl=86_400)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for start, end in _WINDOW.findall(html):
        name = "{}-{}_form13f.zip".format(start, end).lower()
        if name in seen:
            continue
        seen.add(name)
        start_d = pd.to_datetime(start, format="%d%b%Y").date()
        q_end = _period_for(start_d)
        out.append({
            "file": name,
            "url": "{}/files/structureddata/data/form-13f-data-sets/{}".format(BASE, name),
            "window_start": start_d.isoformat(),
            "window_end": pd.to_datetime(end, format="%d%b%Y").date().isoformat(),
            "period_end": q_end.isoformat(),
            "deadline": deadline_for(q_end).isoformat(),
        })
    if not out:
        raise EmptyDataError("SEC's 13F data-set index listed no archives")
    return sorted(out, key=lambda w: w["period_end"], reverse=True)


def _period_for(window_start: date) -> date:
    """The report period a data-set window carries: the end of its first month."""
    following = (window_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def deadline_for(period_end: date) -> date:
    """The 45-day 13F deadline, rolled to the next weekday.

    This is the date the market's aggregate position became knowable — every
    filer above the threshold had to have reported by it — and it is what a
    flow row anchors on. Not the period end (six weeks before anyone outside
    the filers could see the numbers) and not the data-set publication date
    (a fortnight after the filings were already public on EDGAR).
    """
    day = period_end + timedelta(days=45)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def latest_pair() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The two most recent data sets — the newest period and the one before."""
    avail = windows()
    if len(avail) < 2:
        raise EmptyDataError("Fewer than two 13F data sets are published")
    return avail[0], avail[1]


# --------------------------------------------------------------------------- #
# Positions per period
# --------------------------------------------------------------------------- #
def _read(zf: zipfile.ZipFile, member: str, **kwargs: Any) -> pd.DataFrame:
    with zf.open(member) as fh:
        return pd.read_csv(fh, sep="\t", encoding="latin-1", on_bad_lines="skip", **kwargs)


@cached("thirteenf.positions.v2", ttl=TTL_WINDOW)
def positions(url: str, period_end: str) -> Dict[str, Any]:
    """Every filer's long share position in every CUSIP, for one report period.

    ``url`` is the data-set archive; ``period_end`` (ISO) selects the report
    period inside it. Returns ``{"positions": DataFrame, "filers": DataFrame,
    "period_end": ...}`` where positions has one row per (filer, CUSIP) with
    ``shares`` and ``value`` (dollars), and filers maps CIK to name and filing
    date.

    What is dropped, and why: principal-amount rows (``PRN`` — debt), puts and
    calls (options, not the underlying), and 13F-NT notices (the holdings are
    on someone else's report). What is resolved: a filer with several filings
    for the period keeps the newest, a ``RESTATEMENT`` amendment replaces the
    original outright, and a ``NEW HOLDINGS`` amendment is added to it.

    Shared reporting is *not* resolved. A sub-adviser and its parent can both
    list the same shares, and the data set gives no reliable way to net them;
    a quarter-over-quarter change is robust to that as long as both keep
    filing the same way, and the per-filer attribution on a flow row is what
    lets a reader see when they did not.
    """
    try:
        body = fetch(url, headers=_headers(), ttl=None, use_cache=False, retries=2)
    except Exception as exc:  # noqa: BLE001 - an unpublished archive 404s
        raise EmptyDataError("Could not fetch the 13F data set at {}: {}".format(url, exc)) from exc

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        sub = _read(zf, "SUBMISSION.tsv", dtype=str)
        cover = _read(zf, "COVERPAGE.tsv", dtype=str,
                      usecols=["ACCESSION_NUMBER", "ISAMENDMENT", "AMENDMENTTYPE",
                               "FILINGMANAGER_NAME", "REPORTTYPE"])
        info = _read(
            zf, "INFOTABLE.tsv",
            usecols=["ACCESSION_NUMBER", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "VALUE",
                     "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"],
            dtype={"ACCESSION_NUMBER": "category", "NAMEOFISSUER": "category",
                   "TITLEOFCLASS": "category", "CUSIP": "category",
                   "SSHPRNAMTTYPE": "category", "PUTCALL": "category"},
        )

    sub["period"] = pd.to_datetime(sub["PERIODOFREPORT"], format=_DATE_FMT, errors="coerce")
    sub["filed"] = pd.to_datetime(sub["FILING_DATE"], format=_DATE_FMT, errors="coerce")
    wanted_period = pd.Timestamp(period_end)
    sub = sub[(sub["period"] == wanted_period)
              & sub["SUBMISSIONTYPE"].isin(["13F-HR", "13F-HR/A"])]
    sub = sub.merge(cover, on="ACCESSION_NUMBER", how="left")
    if sub.empty:
        raise EmptyDataError("No 13F-HR filings for {} in {}".format(period_end, url))

    # Which accession numbers count for each filer: the newest original (or
    # restatement, which supersedes everything before it) plus any new-holdings
    # amendments filed after it.
    keep: List[str] = []
    for _cik, group in sub.sort_values("filed").groupby("CIK", sort=False):
        amend = group["AMENDMENTTYPE"].fillna("").str.upper()
        base_rows = group[(group["SUBMISSIONTYPE"] == "13F-HR") | (amend == "RESTATEMENT")]
        if base_rows.empty:
            # Only NEW HOLDINGS amendments in this window (the original was
            # in an earlier one) — take them; better partial than nothing.
            keep.extend(group["ACCESSION_NUMBER"].tolist())
            continue
        base = base_rows.iloc[-1]
        keep.append(base["ACCESSION_NUMBER"])
        later = group[(group["filed"] > base["filed"]) & (amend == "NEW HOLDINGS")]
        keep.extend(later["ACCESSION_NUMBER"].tolist())
    kept = sub[sub["ACCESSION_NUMBER"].isin(keep)]

    rows = info[info["ACCESSION_NUMBER"].isin(set(kept["ACCESSION_NUMBER"]))]
    rows = rows[(rows["SSHPRNAMTTYPE"] == "SH") & rows["PUTCALL"].isna()]
    # The class filter runs on the category levels, not the 3.8M rows.
    classes = rows["TITLEOFCLASS"].cat.categories
    common_classes = {c for c in classes if not _NOT_COMMON.search(str(c))}
    rows = rows[rows["TITLEOFCLASS"].isin(common_classes)]
    rows = rows.assign(
        shares=pd.to_numeric(rows["SSHPRNAMT"], errors="coerce"),
        value=pd.to_numeric(rows["VALUE"], errors="coerce"),
    ).dropna(subset=["shares"])
    rows = rows[rows["shares"] > 0]

    acc_to_cik = dict(zip(kept["ACCESSION_NUMBER"], kept["CIK"]))
    rows["filer_cik"] = rows["ACCESSION_NUMBER"].astype(str).map(acc_to_cik)
    rows["cusip"] = rows["CUSIP"].astype(str).str.upper().str.strip()
    # A filer listing the same CUSIP twice (two classes of discretion, say)
    # is one position of the summed size.
    pos = (rows.groupby(["filer_cik", "cusip"], sort=False, observed=True)
               .agg(shares=("shares", "sum"), value=("value", "sum"))
               .reset_index())
    pos["filer_cik"] = pos["filer_cik"].astype("category")
    pos["cusip"] = pos["cusip"].astype("category")
    pos["shares"] = pos["shares"].astype("int64")
    pos["value"] = pos["value"].astype("float64")

    names = (rows.groupby("cusip", sort=False, observed=True)["NAMEOFISSUER"]
                 .agg(lambda s: str(s.iloc[0])).to_dict())
    filers = (kept.sort_values("filed")
                  .drop_duplicates("CIK", keep="last")[["CIK", "FILINGMANAGER_NAME", "filed"]]
                  .rename(columns={"CIK": "filer_cik", "FILINGMANAGER_NAME": "filer"}))
    filers["filed"] = filers["filed"].dt.strftime("%Y-%m-%d")
    filers["passive"] = filers["filer"].fillna("").str.contains(_PASSIVE)
    return {
        "period_end": period_end,
        "positions": pos.reset_index(drop=True),
        "filers": filers.reset_index(drop=True),
        "issuer_names": names,
        "filings": int(len(kept)),
    }


# --------------------------------------------------------------------------- #
# CUSIP -> ticker
# --------------------------------------------------------------------------- #
def _ftd_pairs(year: int, month: int, half: str) -> pd.DataFrame:
    url = "{}/cnsfails{:04d}{:02d}{}.zip".format(FTD, year, month, half)
    body = fetch(url, headers=_headers(), ttl=TTL_REFERENCE)
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            df = pd.read_csv(fh, sep="|", encoding="latin-1", on_bad_lines="skip",
                             usecols=["CUSIP", "SYMBOL", "DESCRIPTION"], dtype=str)
    return df.dropna(subset=["CUSIP", "SYMBOL"]).drop_duplicates(["CUSIP", "SYMBOL"])


@cached("thirteenf.cusip_map.v1", ttl=TTL_REFERENCE)
def cusip_symbol_map(months: int = 4) -> pd.DataFrame:
    """``cusip`` (9-char, upper) -> ``symbol`` for every security that failed to deliver.

    Nearly every exchange-listed security shows a fail somewhere in a few
    months, which makes SEC's twice-monthly fails-to-deliver files the one free
    source that lists CUSIP and ticker side by side across the market. Where a
    CUSIP maps to several symbols (a listing that moved exchanges, a symbol
    reused) the most recent file wins.

    Coverage is best exactly where a small-cap flow screen needs it — thinly
    traded names fail constantly — and worst at mega-caps, which is fine.
    """
    today = date.today()
    frames: List[pd.DataFrame] = []
    for i in range(months):
        y, m = divmod(today.year * 12 + (today.month - 1) - i, 12)
        m += 1
        for half in ("b", "a"):
            try:
                part = _ftd_pairs(y, m, half)
            except Exception:  # noqa: BLE001 - the newest half-month may not exist yet
                continue
            part["order"] = i * 2 + (0 if half == "b" else 1)
            frames.append(part)
    if not frames:
        raise EmptyDataError("No fails-to-deliver files could be read for the CUSIP map")
    df = pd.concat(frames, ignore_index=True)
    df["cusip"] = df["CUSIP"].str.upper().str.strip()
    df["symbol"] = df["SYMBOL"].str.upper().str.strip()
    df = df.sort_values("order").drop_duplicates("cusip", keep="first")
    return df[["cusip", "symbol", "DESCRIPTION"]].rename(
        columns={"DESCRIPTION": "description"}).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# The flow table
# --------------------------------------------------------------------------- #
@cached("thirteenf.flows.v3", ttl=TTL_WINDOW)
def flows(current_url: str, current_period: str,
          prior_url: str, prior_period: str,
          top_n: int = 5) -> pd.DataFrame:
    """Per-CUSIP change in reported institutional position between two periods.

    One row per CUSIP with:

    ``shares_now`` / ``shares_prior``
        Total reported shares across the filers present in **both** periods.
        Filers that appear or disappear between the two are counted separately
        (``entering_filer_shares`` / ``departing_filer_shares``) and kept out
        of ``net_change`` — a manager crossing the reporting threshold is not
        a trade.
    ``net_change``
        ``shares_now - shares_prior`` over the common filers; the flow.
    ``positions_opened`` / ``positions_closed``
        Common filers that went from zero to a position, and the reverse.
    ``top_buyers`` / ``top_sellers``
        The ``top_n`` largest per-filer changes each way, with the shares that
        filer still held at period end — the part of an exit that has not
        happened yet — and whether the filer is an index manager.
    ``passive_share``
        Share of gross flow (sum of absolute per-filer changes) from index
        managers.
    ``implied_price``
        Aggregate reported value over aggregate shares — the price filers
        marked the position at, i.e. the period-end price.
    """
    now = positions(current_url, current_period)
    then = positions(prior_url, prior_period)
    a, b = now["positions"], then["positions"]
    common = set(a["filer_cik"].astype(str)) & set(b["filer_cik"].astype(str))

    a_c = a[a["filer_cik"].astype(str).isin(common)]
    b_c = b[b["filer_cik"].astype(str).isin(common)]
    merged = a_c[["filer_cik", "cusip", "shares", "value"]].astype({"filer_cik": str, "cusip": str}).merge(
        b_c[["filer_cik", "cusip", "shares"]].astype({"filer_cik": str, "cusip": str}),
        on=["filer_cik", "cusip"], how="outer", suffixes=("_now", "_prior"),
    )
    merged["shares_now"] = merged["shares_now"].fillna(0).astype("int64")
    merged["shares_prior"] = merged["shares_prior"].fillna(0).astype("int64")
    merged["change"] = merged["shares_now"] - merged["shares_prior"]

    filers = now["filers"].astype({"filer_cik": str}).set_index("filer_cik")
    passive = filers["passive"].to_dict()
    merged["passive"] = merged["filer_cik"].map(passive).fillna(False).astype(bool)

    grouped = merged.groupby("cusip", sort=False)
    out = grouped.agg(
        shares_now=("shares_now", "sum"),
        shares_prior=("shares_prior", "sum"),
        net_change=("change", "sum"),
        gross_flow=("change", lambda s: int(s.abs().sum())),
        filers_now=("shares_now", lambda s: int((s > 0).sum())),
        filers_prior=("shares_prior", lambda s: int((s > 0).sum())),
        value_now=("value", "sum"),
    ).reset_index()
    opened = merged[(merged["shares_prior"] == 0) & (merged["shares_now"] > 0)].groupby("cusip").size()
    closed = merged[(merged["shares_prior"] > 0) & (merged["shares_now"] == 0)].groupby("cusip").size()
    passive_flow = merged[merged["passive"]].groupby("cusip")["change"].agg(lambda s: int(s.abs().sum()))
    out["positions_opened"] = out["cusip"].map(opened).fillna(0).astype(int)
    out["positions_closed"] = out["cusip"].map(closed).fillna(0).astype(int)
    gross = out["gross_flow"].astype("float64").replace(0.0, float("nan"))
    held = out["shares_now"].astype("float64").replace(0.0, float("nan"))
    out["passive_share"] = (out["cusip"].map(passive_flow).fillna(0).astype("float64") / gross).round(4)
    out["implied_price"] = out["value_now"].astype("float64") / held

    # Filers that were not in both periods: their shares are context, not flow.
    entering = a[~a["filer_cik"].astype(str).isin(common)].groupby("cusip", observed=True)["shares"].sum()
    departing = b[~b["filer_cik"].astype(str).isin(common)].groupby("cusip", observed=True)["shares"].sum()
    out["entering_filer_shares"] = out["cusip"].map(entering.rename(index=str)).fillna(0).astype("int64")
    out["departing_filer_shares"] = out["cusip"].map(departing.rename(index=str)).fillna(0).astype("int64")

    # Attribution: the largest movers each way, with what they still hold.
    names = filers["filer"].to_dict()
    filed = filers["filed"].to_dict()

    def _top(sign: int) -> Dict[str, List[Dict[str, Any]]]:
        part = merged[merged["change"] * sign > 0].copy()
        part["rank_key"] = part["change"].abs()
        part = part.sort_values(["cusip", "rank_key"], ascending=[True, False])
        result: Dict[str, List[Dict[str, Any]]] = {}
        for cusip, grp in part.groupby("cusip", sort=False):
            result[cusip] = [
                {"filer": names.get(r.filer_cik, r.filer_cik), "filer_cik": r.filer_cik,
                 "change": int(r.change), "held_now": int(r.shares_now),
                 "held_prior": int(r.shares_prior), "passive": bool(r.passive),
                 "filed": filed.get(r.filer_cik)}
                for r in grp.head(top_n).itertuples()
            ]
        return result

    buyers, sellers = _top(+1), _top(-1)
    out["top_buyers"] = out["cusip"].map(buyers)
    out["top_sellers"] = out["cusip"].map(sellers)
    out["top_buyers"] = out["top_buyers"].apply(lambda v: v if isinstance(v, list) else [])
    out["top_sellers"] = out["top_sellers"].apply(lambda v: v if isinstance(v, list) else [])
    out["issuer"] = out["cusip"].map(now["issuer_names"]).fillna(out["cusip"].map(then["issuer_names"]))
    # Nearly everyone out and nearly nothing left: the security changed
    # identity or stopped trading. Nearly everyone in from nothing: the new
    # identity arriving. Neither is a flow, and both are labelled so.
    # Below a handful of holders the pattern is indistinguishable from one
    # fund selling out, so it is not called an identity change.
    prior_holders = out["filers_prior"].replace(0, 1)
    now_holders = out["filers_now"].replace(0, 1)
    vanished = ((out["filers_prior"] >= MIN_HOLDERS_FOR_IDENTITY)
                & (out["positions_closed"] / prior_holders >= CUSIP_TURNOVER)
                & (out["shares_now"] <= 0.1 * out["shares_prior"]))
    arrived = ((out["filers_now"] >= MIN_HOLDERS_FOR_IDENTITY)
               & (out["positions_opened"] / now_holders >= CUSIP_TURNOVER)
               & (out["shares_prior"] <= 0.1 * out["shares_now"]))
    out["identity_change_suspected"] = vanished | arrived
    out["period_end"] = current_period
    out["prior_period_end"] = prior_period
    out["common_filers"] = len(common)
    return out.reset_index(drop=True)
