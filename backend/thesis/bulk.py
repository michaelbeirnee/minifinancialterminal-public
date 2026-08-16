"""SEC quarterly insider-transaction archives (Form 345 data sets).

One ~10 MB zip per quarter holds every Form 3/4/5 transaction filed in that
quarter, already parsed to TSV — transaction codes, prices, share counts,
reporting-owner relationships and the 10b5-1 flag. That makes it the only
practical source for the *denominators* the scanner needs: how often a given
insider buys, how large their buys usually are, and what a cluster looks like
across the whole market.

It is useless for fresh signal. The archive is published roughly a quarter in
arrears (checked 2026-08-12: ``2026q1`` served, ``2026q2`` still 404), so live
detection has to come from the daily index instead. Bulk is history; daily is
now. They are seamed on *filing* date, never trade date — the archives are
organised by the quarter a filing was filed in, so a filing-date watermark
partitions the two sources with no gap and no overlap.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..core.caching import TTL_REFERENCE, cached
from ..core.errors import EmptyDataError
from ..core.http import fetch
from ..config import settings

BASE = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets"

#: Members we actually read. FOOTNOTES.tsv is the largest file in the archive
#: (~30 MB) and is needed despite that: most ownership-nature text is inline on
#: NONDERIV_TRANS, but a substantial minority is a pointer instead (in 2025Q2,
#: 488 of 6,414 open-market purchases), and those are exactly the indirect
#: holdings the automatic-vehicle filter has to inspect.
_MEMBERS = ("SUBMISSION.tsv", "REPORTINGOWNER.tsv", "NONDERIV_TRANS.tsv", "FOOTNOTES.tsv")

#: The archive stamps dates as ``30-MAY-2025``.
_DATE_FMT = "%d-%b-%Y"

#: ``AFF10B5ONE`` is written five different ways across filers and years.
#: Blank means *unknown*, which is not the same as False — coercing it would
#: silently treat unknown-plan trades as discretionary.
_TRUE = {"1", "true", "TRUE", "Y", "y"}
_FALSE = {"0", "false", "FALSE", "N", "n"}

#: Indirect holdings that are payroll or plan mechanics rather than a decision.
_AUTO_VEHICLE = re.compile(
    r"401\(?K|PROFIT.?SHARING|ESPP|EMPLOYEE STOCK PURCHASE|DIVIDEND REINVEST"
    r"|\bDRIP\b|SAVINGS PLAN|DEFERRED COMP|RETIREMENT PLAN",
    re.IGNORECASE,
)


def _headers() -> Dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def quarter_url(year: int, quarter: int) -> str:
    return "{}/{}q{}_form345.zip".format(BASE, year, quarter)


def _tri_state(value: Any) -> Optional[bool]:
    """``'1'/'true' -> True``, ``'0'/'false' -> False``, blank -> ``None``."""
    text = str(value).strip()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _roles(relationship: Any, title: Any) -> Dict[str, Any]:
    """Normalise REPORTINGOWNER's relationship column into role flags.

    The column packs one or more roles into a single string; a person who is
    both an officer and a director shows up with both.
    """
    text = str(relationship or "").lower()
    return {
        "is_officer": "officer" in text,
        "is_director": "director" in text,
        "is_ten_pct": "tenpercent" in text or "ten percent" in text,
        "is_other": "other" in text,
        "title": str(title or "").strip(),
    }


def _footnote_lookup(notes: pd.DataFrame) -> Dict[Tuple[str, str], str]:
    """``(accession, footnote_id) -> text``."""
    if notes.empty:
        return {}
    return {
        (str(a).strip(), str(f).strip()): str(t)
        for a, f, t in zip(notes["ACCESSION_NUMBER"], notes["FOOTNOTE_ID"], notes["FOOTNOTE_TXT"])
        if pd.notna(t)
    }


def _resolved_nature(df: pd.DataFrame, notes: Dict[Tuple[str, str], str]) -> pd.Series:
    """Ownership-nature text with footnote pointers dereferenced.

    Most rows carry the text inline, but a meaningful minority say only "see
    footnote" and put the substance in FOOTNOTES.tsv. Only the footnote the row
    actually references is pulled in — concatenating every footnote on the
    filing would let an unrelated note about a gift or a sale trigger the
    automatic-vehicle exclusion and silently drop a real purchase.
    """
    inline = df.get("NATURE_OF_OWNERSHIP", pd.Series([""] * len(df), index=df.index)).fillna("")
    pointers = df.get("NATURE_OF_OWNERSHIP_FN", pd.Series([None] * len(df), index=df.index))

    resolved: List[str] = []
    for accession, text, pointer in zip(df["ACCESSION_NUMBER"], inline, pointers):
        text = str(text).strip()
        if pd.isna(pointer):
            resolved.append(text)
            continue
        key = str(accession).strip()
        extra = [
            notes.get((key, fid.strip()), "")
            for fid in str(pointer).split(",")
            if fid.strip()
        ]
        resolved.append(" ".join([text] + [e for e in extra if e]).strip())
    return pd.Series(resolved, index=df.index)


@cached("thesis.bulk.quarter.v2", ttl=TTL_REFERENCE)
def quarter(year: int, quarter_no: int) -> pd.DataFrame:
    """One quarter of non-derivative insider transactions, normalised.

    Returns one row per raw transaction line — *before* any collapsing, so the
    caller can see the filing artefacts (multi-tranche fills, affiliate
    double-filings) that :mod:`backend.thesis.collapse` folds away.

    Raises :class:`EmptyDataError` when the quarter is not published yet, which
    for the current and previous quarter is the normal state, not a failure.
    """
    url = quarter_url(year, quarter_no)
    try:
        body = fetch(url, headers=_headers(), ttl=TTL_REFERENCE)
    except Exception as exc:  # noqa: BLE001 - an unpublished quarter 404s
        raise EmptyDataError(
            "SEC has not published {}q{} yet ({})".format(year, quarter_no, exc)
        ) from exc

    frames: Dict[str, pd.DataFrame] = {}
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = set(zf.namelist())
        for member in _MEMBERS:
            if member not in names:
                raise EmptyDataError("{}q{} archive is missing {}".format(year, quarter_no, member))
            with zf.open(member) as fh:
                frames[member] = pd.read_csv(
                    fh, sep="\t", dtype=str, encoding="latin-1", on_bad_lines="skip"
                )

    sub = frames["SUBMISSION.tsv"]
    own = frames["REPORTINGOWNER.tsv"]
    trans = frames["NONDERIV_TRANS.tsv"]
    notes = _footnote_lookup(frames["FOOTNOTES.tsv"])

    # A filing can name several reporting owners. Keep them all: the affiliate
    # collapse needs to see the duplicates in order to fold them.
    df = trans.merge(sub, on="ACCESSION_NUMBER", how="inner", suffixes=("", "_sub"))
    df = df.merge(own, on="ACCESSION_NUMBER", how="inner", suffixes=("", "_own"))
    if df.empty:
        raise EmptyDataError("{}q{} joined to zero rows".format(year, quarter_no))

    roles = [_roles(r, t) for r, t in zip(df["RPTOWNER_RELATIONSHIP"], df.get("RPTOWNER_TITLE"))]
    shares = pd.to_numeric(df["TRANS_SHARES"], errors="coerce")
    price = pd.to_numeric(df["TRANS_PRICEPERSHARE"], errors="coerce")
    nature = _resolved_nature(df, notes)

    out = pd.DataFrame(
        {
            "accession": df["ACCESSION_NUMBER"],
            "filing_date": pd.to_datetime(df["FILING_DATE"], format=_DATE_FMT, errors="coerce"),
            "trans_date": pd.to_datetime(df["TRANS_DATE"], format=_DATE_FMT, errors="coerce"),
            "doc_type": df["DOCUMENT_TYPE"].str.strip(),
            "issuer_cik": df["ISSUERCIK"].str.strip(),
            "issuer_name": df["ISSUERNAME"].str.strip(),
            "symbol": df["ISSUERTRADINGSYMBOL"].str.strip().str.upper(),
            "owner_cik": df["RPTOWNERCIK"].str.strip(),
            "owner_name": df["RPTOWNERNAME"].str.strip(),
            "is_officer": [r["is_officer"] for r in roles],
            "is_director": [r["is_director"] for r in roles],
            "is_ten_pct": [r["is_ten_pct"] for r in roles],
            "is_other": [r["is_other"] for r in roles],
            "title": [r["title"] for r in roles],
            "code": df["TRANS_CODE"].str.strip().str.upper(),
            "shares": shares,
            "price": price,
            "value_usd": shares * price,
            "acq_disp": df["TRANS_ACQUIRED_DISP_CD"].str.strip().str.upper(),
            "shares_after": pd.to_numeric(df["SHRS_OWND_FOLWNG_TRANS"], errors="coerce"),
            "ownership_form": df["DIRECT_INDIRECT_OWNERSHIP"].str.strip().str.upper(),
            "nature": nature.str.strip(),
            "aff10b5one": [_tri_state(v) for v in df["AFF10B5ONE"]],
        }
    )
    out["auto_vehicle"] = out["nature"].str.contains(_AUTO_VEHICLE, na=False)
    out["quarter"] = "{}Q{}".format(year, quarter_no)
    return out.reset_index(drop=True)


def available_quarters(start_year: int = 2015, today: Optional[date] = None) -> List[Tuple[int, int]]:
    """Quarters that plausibly exist, newest last.

    Cheap arithmetic rather than probing: the caller discovers the real
    watermark by hitting :func:`quarter` and catching ``EmptyDataError``.
    """
    today = today or date.today()
    out: List[Tuple[int, int]] = []
    for year in range(start_year, today.year + 1):
        for q in range(1, 5):
            if year == today.year and (q - 1) * 3 + 1 > today.month:
                continue
            out.append((year, q))
    return out


def load(start: Tuple[int, int], end: Optional[Tuple[int, int]] = None) -> pd.DataFrame:
    """Concatenate a run of quarters, skipping any that are not published.

    Missing quarters are skipped rather than raised: the newest one or two are
    expected to be absent, and a gap in the middle is better surfaced by the
    caller inspecting the returned ``quarter`` column than by an exception.
    """
    wanted = [
        (y, q)
        for (y, q) in available_quarters(start_year=start[0])
        if (y, q) >= start and (end is None or (y, q) <= end)
    ]
    frames: List[pd.DataFrame] = []
    for year, q in wanted:
        try:
            frames.append(quarter(year, q))
        except EmptyDataError:
            continue
    if not frames:
        raise EmptyDataError("No published quarters in the requested range")
    return pd.concat(frames, ignore_index=True)


@cached("thesis.bulk.owner_relations.v1", ttl=TTL_REFERENCE)
def owner_relations(year: int, quarter_no: int) -> pd.DataFrame:
    """Every (person, company, role) named in one quarter's Form 3/4/5 filings.

    Built from SUBMISSION x REPORTINGOWNER alone — deliberately *not* the
    transaction join :func:`quarter` returns, because a filer appears there
    only if the filing carried a non-derivative transaction. A director whose
    filings are all derivative-table RSU grants (a common shape for board
    members) would be invisible to the transaction join, and the board-seat
    resolver would miss exactly the person it exists to find.
    """
    url = quarter_url(year, quarter_no)
    try:
        body = fetch(url, headers=_headers(), ttl=TTL_REFERENCE)
    except Exception as exc:  # noqa: BLE001 - an unpublished quarter 404s
        raise EmptyDataError(
            "SEC has not published {}q{} yet ({})".format(year, quarter_no, exc)
        ) from exc

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        with zf.open("SUBMISSION.tsv") as fh:
            sub = pd.read_csv(fh, sep="\t", dtype=str, encoding="latin-1",
                              usecols=["ACCESSION_NUMBER", "ISSUERCIK"],
                              on_bad_lines="skip")
        with zf.open("REPORTINGOWNER.tsv") as fh:
            own = pd.read_csv(fh, sep="\t", dtype=str, encoding="latin-1",
                              usecols=["ACCESSION_NUMBER", "RPTOWNERCIK",
                                       "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP"],
                              on_bad_lines="skip")

    df = own.merge(sub, on="ACCESSION_NUMBER", how="inner")
    rel = df["RPTOWNER_RELATIONSHIP"].fillna("").str.lower()
    out = pd.DataFrame({
        "owner_cik": df["RPTOWNERCIK"].str.strip(),
        "owner_name": df["RPTOWNERNAME"].str.strip(),
        "issuer_cik": df["ISSUERCIK"].str.strip(),
        "is_officer": rel.str.contains("officer", na=False),
        "is_director": rel.str.contains("director", na=False),
    })
    return out.drop_duplicates().reset_index(drop=True)
