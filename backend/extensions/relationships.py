"""Relationships menu: who a company buys from, sells to, and competes with.

The screen this feeds answers a question a price chart cannot: when this
company's earnings move, whose else move with them? Three sources, all free,
all primary:

* other companies' annual reports, which name this one as a concentration risk,
* this company's own annual report, which names theirs,
* and, for the comparables node, the blended peer group in
  :mod:`backend.providers.peers` — which is itself part filing-mined.

See :mod:`backend.providers.supplychain` for how the filings are read.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import one_symbol
from ..providers import peers as peer_source
from ..providers import sec, supplychain, yahoo

_COLUMNS = ["relationship", "symbol", "company", "exposure_pct", "exposure_basis",
            "pct_of", "disclosed_by", "quote", "form", "filing_date", "filing_url",
            "cik", "disclosures"]


@command("/equity/relationships/counterparties", providers=("sec",),
         summary="Companies whose filings disclose a quantified relationship with this one")
def counterparties(symbol: str, years: int = 4, limit: int = 15,
                   provider: Optional[str] = None) -> Result:
    """Read what *other* filers say about this company.

    A supplier discloses its customer concentration ("sales to X were 27% of our
    net sales"); a distributor discloses its vendor concentration. Both name the
    company on the other side and put a number on it, which is what this returns
    — one row per counterparty, with the sentence and a link to the filing.

    ``exposure_pct`` is a share of the *counterparty's* books, not this
    company's: ``pct_of`` says whose.
    """
    src = resolve_provider(provider, ("sec",))
    sym = one_symbol(symbol)
    df = supplychain.counterparties(sym, years=years, limit=limit)
    return Result(df, provider=src, extra=_subject_extra(sym))


@command("/equity/relationships/disclosed", providers=("sec",),
         summary="Counterparties this company names in its own annual report")
def disclosed(symbol: str, limit: int = 15, provider: Optional[str] = None) -> Result:
    """Read what *this* filer says about everyone else.

    The mirror of ``counterparties``. Here ``exposure_pct`` is a share of this
    company's own books. Frequently empty: a filer must disclose that a customer
    crossed 10% of revenue, but it does not have to say who, and most large caps
    do not.
    """
    src = resolve_provider(provider, ("sec",))
    sym = one_symbol(symbol)
    df = supplychain.subject_disclosures(sym, limit=limit)
    return Result(df, provider=src, extra=_subject_extra(sym))


@command("/equity/relationships/graph", providers=("sec",),
         summary="Suppliers, customers and comparables around one company")
def graph(symbol: str, years: int = 4, limit: int = 12, peers: int = 6,
          provider: Optional[str] = None) -> Result:
    """Everything around one company in a single call, for the exposure map.

    Merges both filing directions and adds the industry comparables. Rows carry
    ``relationship`` — ``supplier``, ``customer`` or ``peer`` — always read from
    this company's point of view: a ``supplier`` row is a company that sells
    *to* this one.

    The two filing sources are gathered concurrently and neither is fatal: a
    company with no disclosed relationships still returns its comparables, and
    ``extra.sources`` reports what each leg contributed or why it was empty.
    """
    src = resolve_provider(provider, ("sec",))
    sym = one_symbol(symbol)

    legs: Dict[str, Callable[[], pd.DataFrame]] = {
        "counterparty_filings": lambda: supplychain.counterparties(sym, years=years, limit=limit),
        "own_filing": lambda: supplychain.subject_disclosures(sym, limit=limit),
        "peers": lambda: _peers(sym, peers),
    }
    with ThreadPoolExecutor(max_workers=len(legs)) as pool:
        gathered = dict(zip(legs, pool.map(_safely, legs.values())))

    collected: List[Dict[str, Any]] = []
    sources: Dict[str, Any] = {}
    warnings: List[str] = []
    for name, (frame, error) in gathered.items():
        sources[name] = {"rows": 0 if frame is None else len(frame), "error": error}
        if error:
            warnings.append("{}: {}".format(name, error))
        if frame is not None and not frame.empty:
            # Records rather than concat: the legs disagree about which columns
            # are populated (comparables carry no percentage), and concatenating
            # frames with all-empty columns leaves pandas guessing at dtypes.
            collected.extend(frame.to_dict("records"))
    if not collected:
        raise EmptyDataError(
            "Nothing links {} to another public company: no filing in the last {} "
            "years names it in a concentration disclosure, its own annual report "
            "names no counterparty, and it has no industry classification."
            .format(sym, years)
        )

    merged = _merge(pd.DataFrame(collected), sym)
    extra = _subject_extra(sym)
    extra.update(
        sources=sources,
        counts={side: int((merged["relationship"] == side).sum())
                for side in ("supplier", "customer", "peer")},
    )
    return Result(merged, provider=src, warnings=warnings, extra=extra)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _safely(fetch: Callable[[], pd.DataFrame]) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Run one leg, turning its failure into a reportable string.

    Each leg is independently allowed to come back empty — a company nobody
    names is a normal outcome here, not an error worth losing the other two
    legs over.
    """
    try:
        return fetch(), None
    except Exception as exc:  # noqa: BLE001 - the message is shown to the user
        return None, str(exc)


def _peers(symbol: str, limit: int) -> pd.DataFrame:
    """Comparables, shaped like the filing rows so they can concat.

    The same peer group the Compare mode uses, so the two screens cannot
    disagree about who a company's comparables are — and because that group is
    partly mined from filings, a peer node can carry the filing that named it
    just as a supplier or customer node does.
    """
    rows, meta = peer_source.peer_group(symbol, limit=limit)
    industry = (meta.get("subject") or {}).get("industry")
    return pd.DataFrame([
        {
            "relationship": "peer",
            "symbol": row["symbol"],
            "company": row["company"] or row["symbol"],
            "exposure_pct": None,
            "exposure_basis": industry or (meta.get("subject") or {}).get("sic_description"),
            "pct_of": None,
            "disclosed_by": ", ".join(row["sources"]),
            "quote": row["why"],
            "form": row["form"], "filing_date": row["filed"], "filing_url": row["filing_url"],
            "cik": None, "disclosures": row["mentions"] or None,
        }
        for row in rows
    ])


def _merge(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """One row per company, filings beating classification.

    A company can arrive from more than one leg — its own filing and the
    subject's often describe the same relationship from opposite sides, and it
    may sit in the peer list as well. Keep the best-evidenced row: a disclosed
    percentage outranks a bare industry match, and a bigger disclosure outranks
    a smaller one.
    """
    for column in _COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame = frame[_COLUMNS].copy()
    frame["symbol"] = frame["symbol"].astype("string").str.upper()
    frame = frame[frame["symbol"].notna() & (frame["symbol"] != symbol.upper())]

    frame["_evidence"] = frame["relationship"].ne("peer").astype(int)
    frame["_size"] = pd.to_numeric(frame["exposure_pct"], errors="coerce").fillna(-1)
    frame = (frame.sort_values(["_evidence", "_size"], ascending=False)
                  .drop_duplicates("symbol", keep="first")
                  .drop(columns=["_evidence", "_size"]))

    order = {"supplier": 0, "customer": 1, "peer": 2}
    frame["_side"] = frame["relationship"].map(order).fillna(3)
    frame["_size"] = pd.to_numeric(frame["exposure_pct"], errors="coerce").fillna(-1)
    return (frame.sort_values(["_side", "_size"], ascending=[True, False])
                 .drop(columns=["_side", "_size"])
                 .reset_index(drop=True))


def _subject_extra(symbol: str) -> Dict[str, Any]:
    """Who the map is centred on, as far as each source knows."""
    out: Dict[str, Any] = {"symbol": symbol}
    try:
        out["search_name"], out["aliases"] = supplychain.subject_names(symbol)
    except Exception:  # noqa: BLE001 - naming is context, never the point of the call
        pass
    try:
        out["cik"] = sec.cik_for(symbol)
    except Exception:  # noqa: BLE001
        pass
    try:
        info = yahoo.info(symbol)
        out.update(
            name=info.get("longName") or info.get("shortName"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            market_cap=info.get("marketCap"),
            revenue_ttm=info.get("totalRevenue"),
            gross_margin=info.get("grossMargins"),
            employees=info.get("fullTimeEmployees"),
        )
    except Exception:  # noqa: BLE001
        pass
    return {"subject": out}


# Explicit re-export so the CLI's `help` shows the list rather than a bare module.
__all__: List[str] = ["counterparties", "disclosed", "graph"]
