"""The three financial statements, normalised into one presentable shape.

``sec`` and ``yahoo`` both return statements, and they disagree about almost
everything: SEC gives the filer's own XBRL tags in filing order, Yahoo gives
forty-five vendor-normalised columns in no order at all, and the two use
different names — and different signs — for the same line. Neither is wrong;
neither is a financial statement either, because a statement is an *ordered*
document with subtotals, indentation and a sign convention.

This module supplies that: one canonical line-item schema per statement, an
alias list per line so either provider fills it, a sign convention so cash
outflows are negative whoever reported them, and the handful of derived lines
a reader expects to find (free cash flow, net cash, working capital) marked as
derived rather than passed off as filed.

SEC leads because it is the filer's own audited tagging with a decade of
history behind it. Yahoo is the fallback that covers what SEC's ``us-gaap``
taxonomy cannot: IFRS filers, foreign private issuers, most ADRs.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from ..core.errors import EmptyDataError
from . import sec, yahoo

NAME = "sec"

# A line: (key, label, indent, weight, aliases)
#   indent — 0 is a statement-level line, 1 sits under the one above it
#   weight — "total" prints boldest, "subtotal" is ruled off, "" is an ordinary
#            line, "pershare"/"shares" change how the number is formatted
Line = Tuple[str, str, int, str, Tuple[str, ...]]

INCOME: Tuple[Line, ...] = (
    ("revenue", "Revenue", 0, "total", ("revenue", "total_revenue", "operating_revenue")),
    ("cost_of_revenue", "Cost of revenue", 1, "",
     ("cost_of_revenue", "reconciled_cost_of_revenue")),
    ("gross_profit", "Gross profit", 0, "subtotal", ("gross_profit",)),
    ("research_and_development", "Research & development", 1, "",
     ("research_and_development", "research_and_development_expenses")),
    ("selling_general_and_admin", "Selling, general & administrative", 1, "",
     ("selling_general_and_admin", "selling_general_and_administration")),
    ("total_operating_expenses", "Total operating expenses", 1, "subtotal",
     ("total_operating_expenses", "operating_expense")),
    ("operating_income", "Operating income", 0, "subtotal",
     ("operating_income", "total_operating_income_as_reported")),
    ("interest_expense", "Interest expense", 1, "",
     ("interest_expense", "interest_expense_non_operating")),
    ("other_income", "Other income / (expense)", 1, "",
     ("other_income_expense", "other_non_operating_income_expenses")),
    ("pretax_income", "Pre-tax income", 0, "subtotal", ("pretax_income",)),
    ("income_tax_expense", "Income tax", 1, "", ("income_tax_expense", "tax_provision")),
    ("net_income", "Net income", 0, "total", ("net_income", "net_income_common_stockholders")),
    ("eps_basic", "Earnings per share — basic", 1, "pershare", ("eps_basic", "basic_eps")),
    ("eps_diluted", "Earnings per share — diluted", 1, "pershare",
     ("eps_diluted", "diluted_eps")),
    ("weighted_average_shares_basic", "Weighted average shares — basic", 1, "shares",
     ("weighted_average_shares_basic", "basic_average_shares")),
    ("weighted_average_shares_diluted", "Weighted average shares — diluted", 1, "shares",
     ("weighted_average_shares_diluted", "diluted_average_shares")),
)

BALANCE: Tuple[Line, ...] = (
    ("cash_and_equivalents", "Cash & equivalents", 1, "",
     ("cash_and_equivalents", "cash_and_cash_equivalents")),
    ("short_term_investments", "Short-term investments", 1, "",
     ("short_term_investments", "other_short_term_investments")),
    ("accounts_receivable", "Accounts receivable", 1, "",
     ("accounts_receivable", "receivables")),
    ("inventory", "Inventory", 1, "", ("inventory",)),
    ("total_current_assets", "Total current assets", 0, "subtotal",
     ("total_current_assets", "current_assets")),
    ("property_plant_equipment", "Property, plant & equipment", 1, "",
     ("property_plant_equipment", "net_ppe")),
    ("goodwill", "Goodwill", 1, "", ("goodwill",)),
    ("intangible_assets", "Intangible assets", 1, "",
     ("intangible_assets", "other_intangible_assets")),
    ("long_term_investments", "Long-term investments", 1, "",
     ("long_term_investments", "investments_and_advances")),
    ("total_assets", "Total assets", 0, "total", ("total_assets",)),
    ("accounts_payable", "Accounts payable", 1, "", ("accounts_payable", "payables")),
    ("short_term_debt", "Short-term debt", 1, "", ("short_term_debt", "current_debt")),
    ("deferred_revenue", "Deferred revenue", 1, "",
     ("deferred_revenue", "current_deferred_revenue")),
    ("total_current_liabilities", "Total current liabilities", 0, "subtotal",
     ("total_current_liabilities", "current_liabilities")),
    ("long_term_debt", "Long-term debt", 1, "", ("long_term_debt",)),
    ("total_liabilities", "Total liabilities", 0, "total",
     ("total_liabilities", "total_liabilities_net_minority_interest")),
    ("common_stock_and_apic", "Common stock & paid-in capital", 1, "",
     ("common_stock_and_apic", "capital_stock", "additional_paid_in_capital")),
    ("retained_earnings", "Retained earnings", 1, "", ("retained_earnings",)),
    ("accumulated_oci", "Accumulated other comprehensive income", 1, "",
     ("accumulated_oci", "gains_losses_not_affecting_retained_earnings")),
    ("total_equity", "Total shareholders' equity", 0, "total",
     ("total_equity", "stockholders_equity", "total_equity_gross_minority_interest")),
    ("total_liabilities_and_equity", "Total liabilities & equity", 0, "subtotal",
     ("total_liabilities_and_equity",)),
)

# The balance sheet is three sections, and each ends on its own total: after
# "Total assets" comes the liabilities, after "Total liabilities" the equity.
BALANCE_OPENS_WITH = "Assets"
BALANCE_SECTION_AFTER: Dict[str, str] = {
    "total_assets": "Liabilities",
    "total_liabilities": "Shareholders' equity",
}

CASH: Tuple[Line, ...] = (
    ("net_income", "Net income", 1, "", ("net_income", "net_income_from_continuing_operations")),
    ("depreciation_and_amortization", "Depreciation & amortisation", 1, "",
     ("depreciation_and_amortization", "depreciation_amortization_depletion")),
    ("stock_based_compensation", "Stock-based compensation", 1, "",
     ("stock_based_compensation",)),
    ("deferred_income_tax", "Deferred income tax", 1, "",
     ("deferred_income_tax", "deferred_tax")),
    ("change_in_working_capital", "Change in working capital", 1, "",
     ("change_in_working_capital",)),
    ("operating_cash_flow", "Cash from operations", 0, "total",
     ("operating_cash_flow", "cash_flow_from_continuing_operating_activities")),
    ("capital_expenditure", "Capital expenditure", 1, "",
     ("capital_expenditure", "capital_expenditure_reported")),
    ("acquisitions", "Acquisitions", 1, "", ("acquisitions", "purchase_of_business")),
    ("investing_cash_flow", "Cash from investing", 0, "total",
     ("investing_cash_flow", "cash_flow_from_continuing_investing_activities")),
    ("dividends_paid", "Dividends paid", 1, "", ("dividends_paid", "cash_dividends_paid")),
    ("share_repurchase", "Share repurchases", 1, "",
     ("share_repurchase", "repurchase_of_capital_stock")),
    ("debt_issued", "Debt issued", 1, "", ("debt_issued", "issuance_of_debt")),
    ("debt_repaid", "Debt repaid", 1, "", ("debt_repaid", "repayment_of_debt")),
    ("financing_cash_flow", "Cash from financing", 0, "total",
     ("financing_cash_flow", "cash_flow_from_continuing_financing_activities")),
    ("net_change_in_cash", "Net change in cash", 0, "subtotal",
     ("net_change_in_cash", "changes_in_cash")),
)

SCHEMA: Dict[str, Tuple[Line, ...]] = {"income": INCOME, "balance": BALANCE, "cash": CASH}

# Money leaving the business. SEC tags these as positive amounts *paid* while
# Yahoo signs them negative; a statement that mixes the two conventions cannot
# be added up by eye, so they are all forced negative.
OUTFLOWS = frozenset((
    "capital_expenditure", "acquisitions", "dividends_paid",
    "share_repurchase", "debt_repaid",
))


def _aliases_weighted(*weights: str) -> frozenset:
    """Every provider column feeding a line of one of ``weights``."""
    return frozenset(
        alias
        for lines in SCHEMA.values()
        for _key, _label, _indent, weight, aliases in lines
        if weight in weights
        for alias in aliases
    )


# Two quarters of revenue add up to a half-year of revenue; two quarters of
# earnings *per share* do not add up to anything, because each was divided by a
# different share count. Kept as sets of provider columns so a new alias on a
# per-share line is covered without being listed twice.
PERSHARE_COLUMNS = _aliases_weighted("pershare")
SHARE_COUNT_COLUMNS = _aliases_weighted("shares")


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def _period_key(value: Any) -> str:
    # A filed period is a timestamp and becomes its ISO date. The year-to-date
    # view indexes by column name instead, and those are already keys — cutting
    # them to ten characters would quietly rename "projected_fy".
    if isinstance(value, str):
        return value
    return str(value)[:10]


def _fetch(symbol: str, kind: str, period: str, limit: int,
           provider: str) -> pd.DataFrame:
    if provider == "yahoo":
        frame = yahoo.statement(symbol, kind, period)
        return frame.tail(limit)
    return sec.statement(symbol, kind, period, limit)


def _pick(frame: pd.DataFrame, aliases: Sequence[str]) -> Tuple[Optional[str], Optional[pd.Series]]:
    """First alias the provider actually reported, and its numbers."""
    for alias in aliases:
        if alias in frame.columns:
            series = pd.to_numeric(frame[alias], errors="coerce")
            if series.notna().any():
                return alias, series
    return None, None


def _series(frame: pd.DataFrame, aliases: Sequence[str]) -> Optional[pd.Series]:
    return _pick(frame, aliases)[1]


def _derived(kind: str, values: Dict[str, pd.Series],
             extra: Dict[str, pd.Series]) -> List[Tuple[str, str, int, str, pd.Series]]:
    """Lines a reader expects that no filer tags directly.

    Kept separate and flagged so nothing computed here can be mistaken for
    something filed.
    """
    out: List[Tuple[str, str, int, str, pd.Series]] = []

    def have(*keys: str) -> bool:
        return all(k in values and values[k].notna().any() for k in keys)

    if kind == "income":
        if not have("gross_profit") and have("revenue", "cost_of_revenue"):
            out.append(("gross_profit_derived", "Gross profit", 0, "subtotal",
                        values["revenue"] - values["cost_of_revenue"]))
        dna = extra.get("depreciation_and_amortization")
        if have("operating_income") and dna is not None and dna.notna().any():
            out.append(("ebitda", "EBITDA", 0, "",
                        values["operating_income"].add(dna, fill_value=0)))
    elif kind == "balance":
        debt = None
        for key in ("short_term_debt", "long_term_debt"):
            if have(key):
                debt = values[key] if debt is None else debt.add(values[key], fill_value=0)
        if debt is not None:
            out.append(("total_debt", "Total debt", 0, "", debt))
            liquid = None
            for key in ("cash_and_equivalents", "short_term_investments"):
                if have(key):
                    liquid = values[key] if liquid is None else liquid.add(values[key], fill_value=0)
            if liquid is not None:
                out.append(("net_cash", "Net cash / (debt)", 0, "", liquid - debt))
        if have("total_current_assets", "total_current_liabilities"):
            out.append(("working_capital", "Working capital", 0, "",
                        values["total_current_assets"] - values["total_current_liabilities"]))
    elif kind == "cash":
        if have("operating_cash_flow", "capital_expenditure"):
            # capital_expenditure is already signed negative by then.
            out.append(("free_cash_flow", "Free cash flow", 0, "total",
                        values["operating_cash_flow"] + values["capital_expenditure"]))
    return out


def _rows_for(kind: str, frame: pd.DataFrame, periods: List[str],
              extra: Dict[str, pd.Series]) -> List[Dict[str, Any]]:
    values: Dict[str, pd.Series] = {}
    for key, _label, _indent, _weight, aliases in SCHEMA[kind]:
        series = _series(frame, aliases)
        if series is None:
            continue
        if key in OUTFLOWS:
            series = -series.abs()
        values[key] = series

    section = BALANCE_OPENS_WITH if kind == "balance" else None

    rows: List[Dict[str, Any]] = []

    def emit(key: str, label: str, indent: int, weight: str,
             series: pd.Series, derived: bool) -> None:
        row: Dict[str, Any] = {
            "statement": kind, "line_item": key, "label": label,
            "indent": indent, "weight": weight, "derived": derived,
        }
        if section:
            row["section"] = section
        by_period = {_period_key(idx): val for idx, val in series.items()}
        for period in periods:
            value = by_period.get(period)
            row[period] = None if value is None or pd.isna(value) else float(value)
        if any(row.get(p) is not None for p in periods):
            rows.append(row)

    for key, label, indent, weight, _aliases in SCHEMA[kind]:
        if key in values:
            emit(key, label, indent, weight, values[key], False)
        # A section ends on its own total, so the switch happens after the line
        # that closes it has been emitted — not before.
        if kind == "balance" and key in BALANCE_SECTION_AFTER:
            section = BALANCE_SECTION_AFTER[key]

    section = "Derived" if kind == "balance" else None
    for key, label, indent, weight, series in _derived(kind, values, extra):
        emit(key, label, indent, weight, series, True)
    return rows


def statements(symbol: str, period: str = "annual", limit: int = 8,
               provider: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """All three statements for ``symbol``, ordered and labelled.

    Returns ``(rows, meta)``. Each row is one line item with one column per
    period; ``meta`` reports the periods (newest first), which provider served
    each statement, and anything that came back empty.
    """
    symbol = symbol.upper().strip()
    if period not in ("annual", "quarter"):
        raise ValueError("period must be annual or quarter")
    order = [provider] if provider else ["sec", "yahoo"]

    frames: Dict[str, pd.DataFrame] = {}
    served: Dict[str, Optional[str]] = {}
    errors: Dict[str, str] = {}
    for kind in ("income", "balance", "cash"):
        served[kind] = None
        last = ""
        for source in order:
            try:
                frame = _fetch(symbol, kind, period, limit, source)
            except Exception as exc:  # noqa: BLE001 - fall through to the next source
                last = str(exc)
                continue
            if not frame.empty:
                frames[kind] = frame
                served[kind] = source
                break
        if served[kind] is None:
            errors[kind] = last or "no data"

    if not frames:
        raise EmptyDataError(
            "No filed financial statements for {}. Indexes, ETFs and crypto do "
            "not file with the SEC, and Yahoo has nothing either.".format(symbol)
        )

    # One period axis across all three, newest first, so the columns line up
    # even where one statement is missing a year the others have.
    periods = sorted({_period_key(idx) for frame in frames.values() for idx in frame.index},
                     reverse=True)[:limit]

    # D&A lives on the cash-flow statement but EBITDA is an income-statement line.
    dna = None
    if "cash" in frames:
        dna = _series(frames["cash"], ("depreciation_and_amortization",
                                       "depreciation_amortization_depletion"))
    shared = {"depreciation_and_amortization": dna} if dna is not None else {}

    rows: List[Dict[str, Any]] = []
    for kind in ("income", "balance", "cash"):
        if kind in frames:
            rows.extend(_rows_for(kind, frames[kind], periods, shared))
    if not rows:
        raise EmptyDataError("No recognisable statement lines for {}".format(symbol))

    meta = {
        "symbol": symbol,
        "period": period,
        "periods": periods,
        "provider_by_statement": served,
        "missing": errors,
        "statements": [k for k in ("income", "balance", "cash") if k in frames],
    }
    return rows, meta


# --------------------------------------------------------------------------- #
# Year to date, and where the year is heading
# --------------------------------------------------------------------------- #
# Four columns rather than a period axis: the year so far, the same stretch of
# last year to read it against, an estimate of where the full year lands, and
# the last full year the estimate should be compared to.
YTD = "ytd"
YTD_LY = "ytd_ly"
PROJECTED = "projected_fy"
FY_LY = "fy_ly"
YTD_COLUMNS: Tuple[str, ...] = (YTD, YTD_LY, PROJECTED, FY_LY)

# Which column each one is a change *from*, for the change view: the year so
# far against the same stretch of last year, the estimate against last year in
# full. The other two pairings would be comparing a part-year to a whole one.
YTD_COMPARE = {YTD: YTD_LY, PROJECTED: FY_LY}

# How many completed years the seasonal ratio is taken over.
SEASONAL_YEARS = 3


def _cluster_dates(dates: Sequence[pd.Timestamp], tolerance: int = 10) -> List[pd.Timestamp]:
    """One date per fiscal year end, from three statements that may differ by days."""
    out: List[pd.Timestamp] = []
    for date in sorted(dates):
        if out and (date - out[-1]).days <= tolerance:
            out[-1] = date
        else:
            out.append(date)
    return out


def _nearest(index: Sequence[pd.Timestamp], target: pd.Timestamp,
             tolerance: int = 10) -> Optional[pd.Timestamp]:
    """The index entry for the same fiscal year end, allowing a few days' drift."""
    near = [(abs((pd.Timestamp(i) - target).days), i) for i in index]
    near = [pair for pair in near if pair[0] <= tolerance]
    return min(near)[1] if near else None


def _aggregate(series: pd.Series, window: Sequence[pd.Timestamp], kind: str,
               column: str) -> Optional[float]:
    """One number for a run of quarters, on the basis that line is measured in."""
    values = series.reindex(window)
    if kind == "balance":
        # A balance sheet is a position on a date, not a total over one: the
        # year so far is simply where the company stood at the last quarter end.
        held = values.dropna()
        return float(held.iloc[-1]) if not held.empty else None
    if not values.notna().any():
        return None
    if values.isna().any():
        # A gap in the window is either a period the provider left out because
        # the line was nil — which adds nothing and can be ignored — or history
        # that does not reach back that far, which would silently make a
        # half-year total out of a single quarter. They are told apart by
        # whether the line was reported at all before the window opens.
        if not series.loc[series.index < window[0]].notna().any():
            return None
    if column in SHARE_COUNT_COLUMNS:
        return float(values.mean())     # already a weighted average per quarter
    return float(values.sum())


def _year_windows(fy_ends: List[pd.Timestamp], index: Sequence[pd.Timestamp],
                  quarters: int, years: int) -> List[Tuple[pd.Timestamp, List[pd.Timestamp]]]:
    """``(fiscal year end, its first N quarter ends)`` for completed years, newest first.

    The window is empty unless all N quarters are there. Yahoo carries about
    five quarters of history, so asking it for the first half of the year
    before last returns one quarter — and a one-quarter total sitting in a
    half-year column is worse than a blank one.
    """
    out: List[Tuple[pd.Timestamp, List[pd.Timestamp]]] = []
    for pos in range(len(fy_ends) - 1, -1, -1):
        end = fy_ends[pos]
        # Before the earliest year end on record there is no boundary to use,
        # so the year is taken as the four quarters that close on it.
        opens = fy_ends[pos - 1] if pos else end - pd.Timedelta(370, unit="D")
        window = [q for q in index if opens < q <= end][-4:]
        out.append((end, window[:quarters] if len(window) >= quarters else []))
        if len(out) >= years:
            break
    return out


def _project(kind: str, column: str, so_far: Optional[float], quarters: int,
             history: Sequence[Tuple[Optional[float], Optional[float]]]) -> Tuple[Optional[float], str]:
    """Where the full year lands if it behaves like the years before it.

    ``history`` is ``(same stretch of that year, that year in full)`` per
    completed year. Their ratio is what the rest of the year has historically
    added, which run-rating cannot know: a retailer three quarters in has taken
    perhaps 60% of its revenue, not 75%, and Apple one quarter in has taken
    rather more than a quarter of its year.

    The median across years rather than the latest one, so a single odd year —
    a 53-week year, an acquisition, a write-off — does not set the estimate.
    """
    if so_far is None or kind == "balance":
        # Projecting a balance sheet means rolling every line forward against
        # the cash flow that produced it. That is a model, not an extrapolation.
        return None, "none"
    if quarters >= 4:
        return so_far, "complete"
    ratios = [
        full / part for part, full in history
        if part and full is not None and not pd.isna(part) and not pd.isna(full)
        and (full > 0) == (part > 0)
    ]
    if ratios:
        return so_far * float(pd.Series(ratios).median()), "seasonal"
    return so_far * 4.0 / quarters, "runrate"


def _ytd_frame(kind: str, quarterly: pd.DataFrame, annual: pd.DataFrame,
               current: List[pd.Timestamp], windows: List[Tuple[pd.Timestamp, List[pd.Timestamp]]],
               quarters: int) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """The four columns for one statement, shaped like any other period frame.

    Returning a frame indexed by the column keys lets the ordinary assembly run
    over it untouched — same line order, same sign convention, same derived
    lines, computed off the year-to-date figures instead of a filed period.
    """
    columns = [c for c in dict.fromkeys(list(quarterly.columns) + list(annual.columns))
               if c not in ("symbol", "fiscal_period")]
    data: Dict[str, Dict[str, Optional[float]]] = {}
    basis: Dict[str, str] = {}

    for column in columns:
        quarters_seen = (pd.to_numeric(quarterly[column], errors="coerce")
                         if column in quarterly.columns else pd.Series(dtype="float64"))
        filed = (pd.to_numeric(annual[column], errors="coerce")
                 if column in annual.columns else pd.Series(dtype="float64"))

        def over(window: Sequence[pd.Timestamp]) -> Optional[float]:
            if not len(window) or quarters_seen.empty:
                return None
            return _aggregate(quarters_seen, window, kind, column)

        # The full year comes off the annual filing rather than the sum of its
        # quarters: it is the audited number, and fiscal Q4 is only ever a
        # subtraction anyway.
        def full_year(end: pd.Timestamp) -> Optional[float]:
            if filed.empty:
                return None
            at = _nearest(filed.index, end)
            if at is None:
                return None
            value = filed.loc[at]
            return None if pd.isna(value) else float(value)

        so_far = over(current)
        history = [(over(window), full_year(end)) for end, window in windows]
        projected, how = _project(kind, column, so_far, quarters, history)
        basis[column] = how
        data[column] = {
            YTD: so_far,
            YTD_LY: history[0][0] if history else None,
            PROJECTED: projected,
            FY_LY: history[0][1] if history else None,
        }

    frame = pd.DataFrame(
        {column: [values[key] for key in YTD_COLUMNS] for column, values in data.items()},
        index=list(YTD_COLUMNS), dtype="float64",
    )
    return frame, basis


def year_to_date(symbol: str, provider: Optional[str] = None,
                 years: int = SEASONAL_YEARS) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The year so far on all three statements, and where it is heading.

    Interim filings are quarterly, so the year to date is the quarters filed
    since the last fiscal year end added up — except on the balance sheet,
    which is a position and is simply carried at its latest date.

    The estimate for the full year scales the year to date by what the rest of
    the year has added in previous years, falling back to a plain run rate for
    a line with no comparable history. Returns ``(rows, meta)`` in the same
    shape as :func:`statements`, with :data:`YTD_COLUMNS` in place of periods.
    """
    symbol = symbol.upper().strip()
    order = [provider] if provider else ["sec", "yahoo"]
    span = (years + 1) * 4 + 2

    quarterly: Dict[str, pd.DataFrame] = {}
    annual: Dict[str, pd.DataFrame] = {}
    served: Dict[str, Optional[str]] = {}
    errors: Dict[str, str] = {}
    for kind in ("income", "balance", "cash"):
        served[kind] = None
        last = ""
        for source in order:
            try:
                frame = _fetch(symbol, kind, "quarter", span, source)
            except Exception as exc:  # noqa: BLE001 - fall through to the next source
                last = str(exc)
                continue
            if frame.empty:
                continue
            quarterly[kind] = frame
            served[kind] = source
            try:
                filed = _fetch(symbol, kind, "annual", years + 2, source)
            except Exception:  # noqa: BLE001 - the year-ago columns simply go blank
                filed = pd.DataFrame()
            if not filed.empty:
                annual[kind] = filed
            break
        if served[kind] is None:
            errors[kind] = last or "no data"

    if not quarterly:
        raise EmptyDataError(
            "No interim filings for {}. Indexes, ETFs and crypto do not file "
            "with the SEC, and Yahoo has nothing either.".format(symbol)
        )
    if not annual:
        raise EmptyDataError(
            "No annual filings for {}, so there is no fiscal year end to count "
            "the year to date from.".format(symbol)
        )

    # The fiscal year end is a property of the filer, not of one statement, so
    # it is taken across all three — a statement missing a year still lands in
    # the same columns as the others.
    fy_ends = _cluster_dates([pd.Timestamp(i) for f in annual.values() for i in f.index])
    every_quarter = _cluster_dates(
        [pd.Timestamp(i) for f in quarterly.values() for i in f.index], tolerance=5)

    current = [q for q in every_quarter if q > fy_ends[-1]]
    # Four quarters in with no 10-K parsed means the annual data is behind the
    # interim data: close the year off and start counting the next one.
    while len(current) > 4:
        fy_ends.append(current[3])
        current = current[4:]
    if not current:
        raise EmptyDataError(
            "{} has filed nothing since its year ended {}. The year to date "
            "starts again with the next quarterly filing.".format(
                symbol, fy_ends[-1].date())
        )

    quarters = len(current)
    opens, through = fy_ends[-1], current[-1]

    rows: List[Dict[str, Any]] = []
    frames: Dict[str, pd.DataFrame] = {}
    spans: Dict[str, int] = {}
    through_by: Dict[str, str] = {}
    # Keyed by statement as well as line, because net income is on two of them.
    basis_by_line: Dict[Tuple[str, str], str] = {}
    for kind in ("income", "balance", "cash"):
        if kind not in quarterly:
            continue
        index = [pd.Timestamp(i) for i in quarterly[kind].index]
        mine = [q for q in index if q > opens][:quarters]
        # Its own count, not the filer's: one of the three statements can lag a
        # quarter behind the others, and two quarters read against a year-ago
        # three look like a decline that never happened.
        span = len(mine)
        if not span:
            continue
        # Newest window first, and the newest completed year *is* ``opens`` —
        # its first N quarters are the stretch the year so far is read against.
        windows = _year_windows(fy_ends, index, span, years)
        frames[kind], basis = _ytd_frame(
            kind, quarterly[kind], annual.get(kind, pd.DataFrame()), mine, windows, span)
        spans[kind] = span
        through_by[kind] = mine[-1].date().isoformat()
        # How each *shown* line was projected, which is what a reader can act
        # on — the provider columns behind them include plenty that never
        # reach a statement.
        for key, _label, _indent, _weight, aliases in SCHEMA[kind]:
            alias = _pick(frames[kind], aliases)[0]
            if alias and basis.get(alias, "none") != "none":
                basis_by_line[(kind, key)] = basis[alias]

    dna = None
    # D&A crosses from the cash-flow statement to EBITDA on the income
    # statement, so it is only usable when the two cover the same stretch.
    if "cash" in frames and spans.get("cash") == spans.get("income"):
        dna = _series(frames["cash"], ("depreciation_and_amortization",
                                       "depreciation_amortization_depletion"))
    shared = {"depreciation_and_amortization": dna} if dna is not None else {}

    for kind in ("income", "balance", "cash"):
        if kind in frames:
            rows.extend(_rows_for(kind, frames[kind], list(YTD_COLUMNS), shared))
    if not rows:
        raise EmptyDataError("No recognisable statement lines for {}".format(symbol))

    methods: Dict[str, int] = {}
    for row in rows:
        # Derived lines inherit whatever their inputs were projected on, so
        # they are left unmarked rather than claiming a basis of their own.
        how = None if row["derived"] else basis_by_line.get((row["statement"], row["line_item"]))
        row["projection_basis"] = how
        if how:
            methods[how] = methods.get(how, 0) + 1

    prior = _year_windows(fy_ends, every_quarter, quarters, 1)
    prior_through = prior[0][1][-1] if prior and prior[0][1] else None
    closes = fy_ends[-1] + pd.DateOffset(years=1)
    stretch = "{} quarter{}".format(quarters, "" if quarters == 1 else "s")
    opened = opens + pd.Timedelta(1, unit="D")     # the day after the last one closed
    day = lambda d: d.strftime("%d %b %Y")
    labels = {
        YTD: "YTD {}".format(through.strftime("%b %y")),
        YTD_LY: "YTD {}".format(prior_through.strftime("%b %y")) if prior_through else "YTD LY",
        PROJECTED: "FY{}E".format(closes.strftime("%y")),
        FY_LY: "FY{}".format(fy_ends[-1].strftime("%y")),
    }
    titles = {
        YTD: "{} filed since the year opened — {} to {}".format(
            stretch, day(opened), day(through)),
        YTD_LY: "The same {} of the year before, to {}".format(
            stretch, day(prior_through)) if prior_through else "Not filed this far back",
        PROJECTED: "Estimated full year to {}, from the year so far".format(day(closes)),
        FY_LY: "Full year to {}, as filed".format(day(fy_ends[-1])),
    }
    meta = {
        "symbol": symbol,
        "period": "ytd",
        "periods": list(YTD_COLUMNS),
        "period_labels": labels,
        "period_titles": titles,
        "compare_to": dict(YTD_COMPARE),
        "quarters_elapsed": quarters,
        # A statement filing behind the others covers less of the year, and its
        # columns say so rather than borrowing the headline dates.
        "quarters_by_statement": spans,
        "through_by_statement": through_by,
        "fiscal_year_opened": opened.date().isoformat(),
        "ytd_through": through.date().isoformat(),
        "prior_ytd_through": prior_through.date().isoformat() if prior_through else None,
        "projection": {
            "method": max(methods, key=methods.get) if methods else "none",
            "seasonal_years": sum(
                1 for _end, window in _year_windows(fy_ends, every_quarter, quarters, years)
                if window),
            "lines_by_method": methods,
        },
        "provider_by_statement": served,
        "missing": errors,
        "statements": [k for k in ("income", "balance", "cash") if k in frames],
    }
    return rows, meta
