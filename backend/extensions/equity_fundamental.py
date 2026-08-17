"""Equity fundamentals, analyst estimates and ownership.

Two providers with different strengths: ``sec`` reads the filer's own XBRL
facts (audited, deep history, no vendor normalisation) while ``yahoo`` is
quicker and carries analyst-facing fields the filings do not.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..core.errors import EmptyDataError
from ..core.models import Result
from ..core.registry import command, resolve_provider
from ..core.utils import date_window, norm_symbols, one_symbol
from ..providers import sec, segments, statements, yahoo

_FUNDAMENTAL_PROVIDERS = ("sec", "yahoo")


def _statement(symbol: str, kind: str, period: str, limit: int, provider: Optional[str]) -> Result:
    src = resolve_provider(provider, _FUNDAMENTAL_PROVIDERS)
    sym = one_symbol(symbol)
    if src == "yahoo":
        df = yahoo.statement(sym, kind, period)
        df.insert(0, "symbol", sym)
        return Result(df.tail(limit), provider=src, index_name="period_ending")
    return Result(sec.statement(sym, kind, period, limit), provider=src, index_name="period_ending")


@command("/equity/fundamental/income", providers=_FUNDAMENTAL_PROVIDERS,
         summary="Income statement by reporting period")
def income(symbol: str, period: str = "annual", limit: int = 12,
           provider: Optional[str] = None) -> Result:
    return _statement(symbol, "income", period, limit, provider)


@command("/equity/fundamental/balance", providers=_FUNDAMENTAL_PROVIDERS,
         summary="Balance sheet by reporting period")
def balance(symbol: str, period: str = "annual", limit: int = 12,
            provider: Optional[str] = None) -> Result:
    return _statement(symbol, "balance", period, limit, provider)


@command("/equity/fundamental/cash", providers=_FUNDAMENTAL_PROVIDERS,
         summary="Cash-flow statement by reporting period")
def cash(symbol: str, period: str = "annual", limit: int = 12,
         provider: Optional[str] = None) -> Result:
    return _statement(symbol, "cash", period, limit, provider)


@command("/equity/fundamental/statements", providers=_FUNDAMENTAL_PROVIDERS,
         summary="Income statement, balance sheet and cash flow in one ordered table")
def all_statements(symbol: str, period: str = "annual", limit: int = 8,
                   provider: Optional[str] = None) -> Result:
    """The three statements as they are actually presented.

    ``income``/``balance``/``cash`` each return one provider's raw columns;
    this returns all three as a single ordered document — one row per line
    item, one column per period, newest first — with indentation, subtotals
    and a consistent sign convention (cash outflows are negative whichever
    provider reported them).

    A few lines every reader looks for are computed rather than filed — free
    cash flow, net cash, working capital, EBITDA — and carry ``derived: true``.

    Falls back from ``sec`` to ``yahoo`` per statement, so IFRS filers and ADRs
    with no ``us-gaap`` XBRL still return something; ``extra.provider_by_statement``
    says which source served each one.
    """
    if provider:
        resolve_provider(provider, _FUNDAMENTAL_PROVIDERS)
    sym = one_symbol(symbol)
    rows, meta = statements.statements(sym, period=period, limit=limit, provider=provider)
    served = [p for p in meta["provider_by_statement"].values() if p]
    return Result(rows, provider=served[0] if served else None, extra=meta)


@command("/equity/fundamental/revenue_segments", providers=("sec",),
         summary="Revenue by reportable segment, geography and product line")
def revenue_segments(symbol: str, dimension: str = "all", period: str = "annual",
                     limit: int = 6, provider: Optional[str] = None) -> Result:
    """Where the revenue on the income statement actually comes from.

    One row per segment with one column per period — the shape ``statements``
    uses — grouped into the three breakdowns a filer may report: ASC 280
    reportable segments, geography, and product or service lines. Pass
    ``dimension`` to ask for one of them (``business``, ``geographic``,
    ``product``). Each group ends on a ``Total disclosed`` subtotal, and
    ``Total revenue`` closes the table so the split can be read against it.

    Read out of the XBRL in the filings themselves rather than the company-facts
    API, which publishes every fact with its dimensions stripped off and so has
    no segment data in it at all. ``extra.filings`` lists the accessions this
    was built from, and ``revenue_share`` is each segment's share of
    consolidated revenue in the newest period it reports.

    Two things a reader should expect. A breakdown need not add up: segment
    revenue is reported before sales between segments are eliminated, and a
    filer discloses only the split it has — ``extra.dimensions[].coverage``
    gives each group's share of revenue, and anything far from 100% is
    warned about. And filers tag two levels of one axis (Apple's
    Product/Service alongside iPhone, Mac, iPad); the finer split is the one
    kept, with what it replaced listed in ``extra.superseded``.
    """
    src = resolve_provider(provider, ("sec",))
    sym = one_symbol(symbol)
    rows, meta = segments.revenue_segments(sym, period=period, limit=limit,
                                           dimension=dimension)
    return Result(rows, provider=src, warnings=meta.pop("warnings", []), extra=meta)


@command("/equity/fundamental/statements_ytd", providers=_FUNDAMENTAL_PROVIDERS,
         summary="Year to date on all three statements, and an estimate of the full year")
def statements_ytd(symbol: str, provider: Optional[str] = None) -> Result:
    """The year so far, the same stretch of last year, and where the year lands.

    Interim filings are quarterly, so the year to date is the quarters filed
    since the last fiscal year end added up — on the income and cash-flow
    statements. The balance sheet is a position rather than a total, so it is
    carried at its latest quarter end instead of being summed, and is not
    projected: a year-end balance sheet is a roll-forward, which is what
    ``/modeling/*`` is for.

    The full-year estimate scales the year to date by what the rest of the year
    added in each of the last three years, taking the median — a retailer three
    quarters in has booked well under three quarters of its revenue, and a plain
    run rate cannot know that. A line with no comparable history falls back to
    the run rate, and says so in its own ``projection_basis``.

    Columns come back as ``ytd``, ``ytd_ly``, ``projected_fy`` and ``fy_ly``
    rather than dates; ``extra.period_labels`` names them.
    """
    if provider:
        resolve_provider(provider, _FUNDAMENTAL_PROVIDERS)
    sym = one_symbol(symbol)
    rows, meta = statements.year_to_date(sym, provider=provider)
    served = [p for p in meta["provider_by_statement"].values() if p]
    return Result(rows, provider=served[0] if served else None, extra=meta)


def _safe(numerator: Any, denominator: Any) -> Any:
    num = pd.to_numeric(numerator, errors="coerce")
    den = pd.to_numeric(denominator, errors="coerce")
    return (num / den.replace(0, np.nan)) if hasattr(den, "replace") else None


@command("/equity/fundamental/ratios", providers=("sec", "yahoo"),
         summary="Profitability, liquidity, leverage and efficiency ratios")
def ratios(symbol: str, period: str = "annual", limit: int = 12,
           provider: Optional[str] = None) -> Result:
    """Ratios derived from the statements themselves, one row per period."""
    src = resolve_provider(provider, ("sec", "yahoo"))
    sym = one_symbol(symbol)
    def fetch(kind: str) -> pd.DataFrame:
        if src == "sec":
            return sec.statement(sym, kind, period, limit)
        return yahoo.statement(sym, kind, period)

    inc = fetch("income")
    bal = fetch("balance")
    try:
        cf = fetch("cash")
    except Exception:  # noqa: BLE001 - cash flow is the flakiest of the three
        cf = pd.DataFrame(index=inc.index)

    def col(frame: pd.DataFrame, *names: str) -> pd.Series:
        for n in names:
            if n in frame.columns:
                return pd.to_numeric(frame[n], errors="coerce")
        return pd.Series(index=frame.index, dtype="float64")

    revenue = col(inc, "revenue", "total_revenue", "operating_revenue")
    gross = col(inc, "gross_profit")
    op_income = col(inc, "operating_income")
    net_income = col(inc, "net_income", "net_income_common_stockholders")
    assets = col(bal, "total_assets")
    equity = col(bal, "total_equity", "stockholders_equity")
    cur_assets = col(bal, "total_current_assets", "current_assets")
    cur_liab = col(bal, "total_current_liabilities", "current_liabilities")
    inventory = col(bal, "inventory")
    liabilities = col(bal, "total_liabilities", "total_liabilities_net_minority_interest")
    lt_debt = col(bal, "long_term_debt")
    st_debt = col(bal, "short_term_debt", "current_debt")
    ocf = col(cf, "operating_cash_flow", "operating_cash_flow_")
    capex = col(cf, "capital_expenditure")
    interest = col(inc, "interest_expense")

    df = pd.DataFrame(index=inc.index)
    df["symbol"] = sym
    df["gross_margin"] = _safe(gross, revenue)
    df["operating_margin"] = _safe(op_income, revenue)
    df["net_margin"] = _safe(net_income, revenue)
    df["return_on_assets"] = _safe(net_income, assets)
    df["return_on_equity"] = _safe(net_income, equity)
    df["current_ratio"] = _safe(cur_assets, cur_liab)
    df["quick_ratio"] = _safe(cur_assets - inventory.fillna(0), cur_liab)
    df["debt_to_equity"] = _safe(lt_debt.fillna(0) + st_debt.fillna(0), equity)
    df["debt_to_assets"] = _safe(lt_debt.fillna(0) + st_debt.fillna(0), assets)
    df["liabilities_to_assets"] = _safe(liabilities, assets)
    df["asset_turnover"] = _safe(revenue, assets)
    df["interest_coverage"] = _safe(op_income, interest.abs())
    df["free_cash_flow"] = ocf.fillna(0) - capex.abs().fillna(0) if not ocf.empty else np.nan
    df["free_cash_flow_margin"] = _safe(df["free_cash_flow"], revenue)
    df = df.dropna(how="all", subset=[c for c in df.columns if c != "symbol"])
    if df.empty:
        raise EmptyDataError("Not enough statement data to compute ratios for {}".format(sym))
    return Result(df.tail(limit), provider=src, index_name="period_ending")


@command("/equity/fundamental/metrics", providers=("yahoo",),
         summary="Current valuation and quality metrics")
def metrics(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    rows = []
    for sym in norm_symbols(symbol):
        i = yahoo.info(sym)
        rows.append(
            {
                "symbol": sym, "market_cap": i.get("marketCap"),
                "enterprise_value": i.get("enterpriseValue"),
                "trailing_pe": i.get("trailingPE"), "forward_pe": i.get("forwardPE"),
                "peg_ratio": i.get("trailingPegRatio"), "price_to_book": i.get("priceToBook"),
                "price_to_sales": i.get("priceToSalesTrailing12Months"),
                "ev_to_revenue": i.get("enterpriseToRevenue"), "ev_to_ebitda": i.get("enterpriseToEbitda"),
                "profit_margin": i.get("profitMargins"), "operating_margin": i.get("operatingMargins"),
                "gross_margin": i.get("grossMargins"), "ebitda_margin": i.get("ebitdaMargins"),
                "return_on_assets": i.get("returnOnAssets"), "return_on_equity": i.get("returnOnEquity"),
                "revenue_growth": i.get("revenueGrowth"), "earnings_growth": i.get("earningsGrowth"),
                "current_ratio": i.get("currentRatio"), "quick_ratio": i.get("quickRatio"),
                "debt_to_equity": i.get("debtToEquity"), "book_value_per_share": i.get("bookValue"),
                "free_cash_flow": i.get("freeCashflow"), "operating_cash_flow": i.get("operatingCashflow"),
                "beta": i.get("beta"), "dividend_yield": i.get("dividendYield"),
                "payout_ratio": i.get("payoutRatio"),
            }
        )
    return Result(rows, provider=src)


@command("/equity/fundamental/multiples", providers=("yahoo",), summary="Valuation multiples only")
def multiples(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    keep = ("symbol", "market_cap", "enterprise_value", "trailing_pe", "forward_pe", "peg_ratio",
            "price_to_book", "price_to_sales", "ev_to_revenue", "ev_to_ebitda")
    rows = [{k: r.get(k) for k in keep} for r in metrics(symbol, provider="yahoo").data]
    return Result(rows, provider=src)


def _restate_shares(shares: pd.Series) -> pd.Series:
    """Put a filed share count series on one basis, anchored on the newest row.

    XBRL keeps what each filing said, and a filing only restates the quarters it
    prints as comparatives. A company that split therefore has old quarters on
    the old share basis and recent ones on the new, and the step between them
    sits at the filing vintage boundary rather than at the split date -- NVDA's
    jumps at Q2 FY24 for a split that happened eleven months later. Scaling by
    the split calendar would move the wrong rows.

    So the step is found in the data instead: a real quarter changes the diluted
    count by a percent or two, and anything past 1.5x is a change of basis, not
    a buyback. Each step is divided out backwards from the newest row, which is
    the one written on today's basis.
    """
    values = pd.to_numeric(shares, errors="coerce")
    usable = values[values > 0]
    if len(usable) < 2:
        return values
    ordered = usable.sort_index()
    scale, factors = 1.0, {}
    for newer, older in zip(ordered.index[::-1], ordered.index[-2::-1]):
        step = ordered[newer] / ordered[older]
        if step > 1.5 or step < 1 / 1.5:
            # Splits come in whole ratios; snap so a buyback inside the same
            # step does not leak into the correction.
            snapped = round(step) if step > 1.5 else 1 / round(1 / step)
            scale *= snapped if snapped else step
        factors[older] = scale
    return values * pd.Series(factors, dtype="float64").reindex(values.index).fillna(1.0)


def _col(frame: pd.DataFrame, *names: str) -> pd.Series:
    """First column present wins — the two providers name the same line item
    differently (``revenue`` vs ``total_revenue``, ``eps_diluted`` vs
    ``diluted_eps``), so every read goes through here."""
    for n in names:
        if n in frame.columns:
            return pd.to_numeric(frame[n], errors="coerce")
    return pd.Series(index=frame.index, dtype="float64")


# Flows are summed over four quarters; stocks are taken as they stood. Each
# statement is rolled on its own index — the income, balance and cash sheets do
# not always agree on a quarter's end date to the day, and interleaving them
# would break every trailing sum with a half-empty row.
_TTM_SPEC = {
    "income": {
        "flows": {
            "net_income": ("net_income", "net_income_common_stockholders",
                           "net_income_from_continuing_operation_net_minority_interest"),
            "revenue": ("revenue", "total_revenue", "operating_revenue"),
            "operating_income": ("operating_income",),
        },
        "stocks": {
            "shares": ("weighted_average_shares_diluted", "diluted_average_shares"),
        },
    },
    "balance": {
        "flows": {},
        "stocks": {
            "cash": ("cash_and_equivalents", "cash_and_cash_equivalents"),
            "short_term_investments": ("short_term_investments", "other_short_term_investments"),
            "short_term_debt": ("short_term_debt", "current_debt"),
            "long_term_debt": ("long_term_debt",),
            "shares_outstanding": ("ordinary_shares_number", "share_issued"),
        },
    },
    "cash": {
        "flows": {
            "depreciation": ("depreciation_and_amortization", "depreciation_amortization_depletion",
                             "depreciation", "reconciled_depreciation"),
            "operating_cash_flow": ("operating_cash_flow",
                                    "cash_flow_from_continuing_operating_activities"),
            "capital_expenditure": ("capital_expenditure",),
        },
        "stocks": {},
    },
}


@command("/equity/fundamental/multiples_history", providers=_FUNDAMENTAL_PROVIDERS,
         summary="Daily valuation multiples against trailing-twelve-month fundamentals")
def multiples_history(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                      lag_days: int = 45, provider: Optional[str] = None) -> Result:
    """Trailing P/E, P/S, EV/EBITDA, FCF yield and dividend yield, per trading day.

    Three things have to be right for a multiple's history to mean anything, and
    each is a way this can silently lie.

    *When you knew it.* A fundamental is only known once it has been filed, so
    every quarter is held back by ``lag_days`` before it feeds the ratios -- 45
    days sits just past the 10-Q deadline for a large accelerated filer. Without
    the lag each ratio turns clairvoyant in the fortnight around earnings and
    the chart becomes one of hindsight rather than of what the stock cost.

    *Which share basis.* Filed figures are as-reported and prices are adjusted,
    so a stock that split 10-for-1 would otherwise show a P/E ten times too low
    before the split. Prices are pulled unadjusted and both sides are put on
    today's basis with the split history.

    *Whether the quarter is even there.* Filers tag Q4 EPS and share counts
    inconsistently, because the 10-K reports the year rather than the quarter,
    and one gap would break four trailing sums. Trailing EPS is therefore net
    income over diluted shares -- net income is tagged every quarter -- with the
    share count carried forward from the last quarter that reported one, which
    is also what a reader would have had at the time.

    Only trailing multiples are here. A forward multiple is a price over an
    estimate, and nothing in this stack archives what the estimate used to be.
    """
    src = resolve_provider(provider, _FUNDAMENTAL_PROVIDERS)
    sym = one_symbol(symbol)
    start, end = date_window(start_date, end_date)

    # Unadjusted: `auto_adjust` folds in dividends as well as splits, which
    # would drift every historical multiple downward on its own.
    prices = yahoo.history(sym, str(start), str(end), adjusted=False)
    if prices.empty:
        raise EmptyDataError("No price history for {}".format(sym))

    def naive(index: Any) -> pd.DatetimeIndex:
        out = pd.DatetimeIndex(pd.to_datetime(index))
        return out.tz_localize(None) if out.tz is not None else out

    try:
        splits = yahoo.splits(sym).dropna()
        splits = splits[splits > 0]
    except Exception:  # noqa: BLE001 - most symbols never split
        splits = pd.Series(dtype="float64")

    def to_today(index: pd.DatetimeIndex) -> pd.Series:
        """Ratio that carries an *as-filed* figure dated `index` onto today's
        share basis: the product of every split that has happened since.

        Only the filings need it. Yahoo already restates prices and dividends
        for splits even with ``auto_adjust`` off — that switch governs dividend
        adjustment, not splits — so applying this to them would divide twice.
        """
        if splits.empty:
            return pd.Series(1.0, index=index)
        when = naive(splits.index).sort_values()
        ratios = splits.sort_index().to_numpy(dtype="float64")
        suffix = np.append(np.cumprod(ratios[::-1])[::-1], 1.0)
        return pd.Series(suffix[when.searchsorted(index, side="right")], index=index)

    def statement(kind: str) -> pd.DataFrame:
        """A missing balance sheet costs EV/EBITDA, not the whole series."""
        try:
            if src == "sec":
                return sec.statement(sym, kind, "quarter", 44)
            return yahoo.statement(sym, kind, "quarter")
        except Exception:  # noqa: BLE001 - degrade to the ratios we can still form
            return pd.DataFrame()

    daily = pd.DataFrame({
        "date": naive(prices.index).normalize(),
        "close": pd.to_numeric(prices["close"], errors="coerce").to_numpy(),
    }).dropna(subset=["close"]).sort_values("date")

    def trailing(kind: str) -> Optional[pd.DataFrame]:
        frame = statement(kind)
        if frame.empty:
            return None
        spec = _TTM_SPEC[kind]
        index = naive(frame.index)
        block = pd.DataFrame(index=index).sort_index()
        for name, aliases in spec["flows"].items():
            series = _col(frame, *aliases)
            if series.notna().any():
                block[name] = pd.Series(series.to_numpy(), index=index).sort_index().rolling(4).sum()
        for name, aliases in spec["stocks"].items():
            series = _col(frame, *aliases)
            if series.notna().any():
                # Carried forward: the last count actually filed is the one a
                # reader had, and Q4 frequently does not report one.
                block[name] = pd.Series(series.to_numpy(), index=index).sort_index().ffill()
        if block.empty:
            return None
        if "shares" in block:
            # Restate onto one basis first, then carry that basis to today --
            # a split newer than the last filing is in no filing yet.
            restated = _restate_shares(block["shares"])
            newest = restated.dropna().index.max()
            block["shares"] = restated * (to_today(pd.DatetimeIndex([newest])).iloc[0]
                                          if newest is not pd.NaT else 1.0)
        block["known_from"] = block.index + pd.to_timedelta(int(lag_days), unit="D")
        return block.reset_index(drop=True).sort_values("known_from")

    income_ttm = trailing("income")
    if income_ttm is None or "net_income" not in income_ttm:
        raise EmptyDataError("No quarterly income statement for {}".format(sym))

    merged, quarters = daily, 0
    for block in (income_ttm, trailing("balance"), trailing("cash")):
        if block is None:
            continue
        quarters = max(quarters, int(block["known_from"].notna().sum()))
        merged = pd.merge_asof(merged, block, left_on="date", right_on="known_from",
                               direction="backward", suffixes=("", "_dup"))
        merged = merged.drop(columns=[c for c in merged.columns
                                      if c == "known_from" or c.endswith("_dup")])
    merged = merged.set_index("date")

    def series(name: str, default: Optional[float] = None) -> Optional[pd.Series]:
        if name in merged:
            return pd.to_numeric(merged[name], errors="coerce")
        return None if default is None else pd.Series(default, index=merged.index)

    def ratio(numerator: Any, denominator: Any) -> pd.Series:
        num = pd.Series(numerator, index=merged.index, dtype="float64")
        den = pd.Series(denominator, index=merged.index, dtype="float64")
        # A negative denominator makes a multiple that reads like a cheap stock
        # and means the opposite, so those days carry no ratio at all.
        return num / den.where(den > 0)

    shares = series("shares")
    out = pd.DataFrame(index=merged.index)
    out["symbol"] = sym
    out["close"] = merged["close"]

    if shares is not None:
        out["shares_diluted"] = shares
        out["eps_ttm"] = ratio(series("net_income"), shares)
        out["pe_trailing"] = ratio(merged["close"], out["eps_ttm"])
        market_cap = merged["close"] * shares
        out["market_cap"] = market_cap
        out["ps_trailing"] = ratio(market_cap, series("revenue"))

        debt = series("short_term_debt", 0.0).fillna(0) + series("long_term_debt", 0.0).fillna(0)
        liquid = series("cash", 0.0).fillna(0) + series("short_term_investments", 0.0).fillna(0)
        operating, depreciation = series("operating_income"), series("depreciation")
        if operating is not None and depreciation is not None:
            ebitda = operating + depreciation.fillna(0)
            out["ebitda_ttm"] = ebitda
            out["enterprise_value"] = market_cap + debt - liquid
            out["ev_ebitda"] = ratio(out["enterprise_value"], ebitda)

        ocf, capex = series("operating_cash_flow"), series("capital_expenditure")
        if ocf is not None and capex is not None:
            out["fcf_ttm"] = ocf - capex.abs()
            out["fcf_yield"] = ratio(out["fcf_ttm"], market_cap)

    # Dividends are paid, not filed, so they carry no publication lag.
    try:
        paid = yahoo.dividends(sym).dropna()
        paid.index = naive(paid.index).normalize()
        per_share = paid.groupby(level=0).sum()
        aligned = per_share.reindex(out.index).fillna(0.0).rolling("365D").sum()
        out["dividend_ttm"] = aligned
        out["dividend_yield"] = aligned / out["close"].where(out["close"] > 0)
    except Exception:  # noqa: BLE001 - most symbols simply never paid one
        pass

    keep = [c for c in ("pe_trailing", "ps_trailing") if c in out]
    if keep:
        out = out[out[keep].notna().any(axis=1)]
    if out.empty:
        raise EmptyDataError("No overlap between price history and filings for {}".format(sym))
    return Result(out, provider=src, index_name="date",
                  extra={"symbol": sym, "lag_days": lag_days, "quarters": int(quarters),
                         "basis": "trailing twelve months, today's share basis"})


@command("/equity/fundamental/dividends", providers=("yahoo",), summary="Dividend payment history")
def dividends(symbol: str, limit: int = 200, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    series = yahoo.dividends(one_symbol(symbol))
    df = series.to_frame()
    df.index.name = "date"
    df.insert(0, "symbol", one_symbol(symbol))
    return Result(df.tail(limit), provider=src, index_name="date")


@command("/equity/fundamental/splits", providers=("yahoo",), summary="Stock split history")
def splits(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = yahoo.splits(one_symbol(symbol)).to_frame()
    df.index.name = "date"
    return Result(df, provider=src, index_name="date")


@command("/equity/fundamental/earnings", providers=("yahoo",),
         summary="Reported vs expected EPS by quarter")
def earnings(symbol: str, limit: int = 24, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = yahoo.earnings_dates(one_symbol(symbol), limit)
    df.columns = [str(c).strip().lower().replace(" ", "_").replace(".", "") for c in df.columns]
    df.index.name = "date"
    return Result(df, provider=src, index_name="date")


@command("/equity/fundamental/historical_eps", providers=("sec",), summary="Reported EPS history")
def historical_eps(symbol: str, period: str = "quarter", limit: int = 40,
                   provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    df = sec.statement(one_symbol(symbol), "income", period, limit)
    cols = [c for c in ("symbol", "eps_basic", "eps_diluted", "net_income",
                        "weighted_average_shares_diluted") if c in df.columns]
    if len(cols) <= 1:
        raise EmptyDataError("{} does not tag EPS in XBRL".format(symbol))
    return Result(df[cols], provider=src, index_name="period_ending")


@command("/equity/fundamental/shares_outstanding", providers=("yahoo", "sec"),
         summary="Share count over time")
def shares_outstanding(symbol: str, start_date: Optional[str] = None, end_date: Optional[str] = None,
                       provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo", "sec"))
    sym = one_symbol(symbol)
    if src == "sec":
        df = sec.concept(sym, "EntityCommonStockSharesOutstanding", taxonomy="dei")
        return Result(df[["end", "val", "form", "filed"]].rename(
            columns={"end": "date", "val": "shares_outstanding"}), provider=src)
    series = yahoo.shares_full(sym, start_date, end_date)
    df = series.to_frame()
    df.index.name = "date"
    return Result(df, provider=src, index_name="date")


@command("/equity/fundamental/employee_count", providers=("sec", "yahoo"),
         summary="Employee count as filed")
def employee_count(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec", "yahoo"))
    sym = one_symbol(symbol)
    if src == "sec":
        try:
            return Result(sec.employee_count(sym), provider=src)
        except EmptyDataError as exc:
            # Plenty of filers only state headcount in prose, not in XBRL.
            if provider:
                raise
            src, warning = "yahoo", str(exc)
    else:
        warning = ""
    info = yahoo.info(sym)
    return Result({"symbol": sym, "employees": info.get("fullTimeEmployees")}, provider=src,
                  warnings=[warning] if warning else [])


@command("/equity/fundamental/management", providers=("yahoo",), summary="Executive officers and pay")
def management(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    officers = yahoo.info(one_symbol(symbol)).get("companyOfficers") or []
    if not officers:
        raise EmptyDataError("No officer data published for {}".format(symbol))
    rows = [
        {"name": o.get("name"), "title": o.get("title"), "age": o.get("age"),
         "year_born": o.get("yearBorn"), "total_pay": o.get("totalPay"),
         "exercised_value": o.get("exercisedValue"), "unexercised_value": o.get("unexercisedValue")}
        for o in officers
    ]
    return Result(rows, provider=src)


@command("/equity/fundamental/filings", providers=("sec", "yahoo"), summary="Company SEC filings")
def filings(symbol: str, form_type: Optional[str] = None, limit: int = 100,
            start_date: Optional[str] = None, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec", "yahoo"))
    sym = one_symbol(symbol)
    if src == "yahoo":
        return Result(yahoo.sec_filings(sym).head(limit), provider=src)
    return Result(sec.filings(sym, form_type, limit, start_date), provider=src)


@command("/equity/fundamental/xbrl_concept", providers=("sec",),
         summary="Every reported value of one XBRL concept")
def xbrl_concept(symbol: str, tag: str = "Revenues", taxonomy: str = "us-gaap",
                 units: Optional[str] = None, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    df = sec.concept(one_symbol(symbol), tag, taxonomy, units)
    keep = [c for c in ("end", "start", "val", "unit", "form", "fy", "fp", "filed", "frame", "label")
            if c in df.columns]
    return Result(df[keep], provider=src)


@command("/equity/fundamental/xbrl_frame", providers=("sec",),
         summary="One XBRL concept across every filer for a period")
def xbrl_frame(tag: str = "Assets", period: str = "CY2024Q4I", taxonomy: str = "us-gaap",
               unit: str = "USD", limit: int = 500, provider: Optional[str] = None) -> Result:
    """Cross-sectional XBRL data — the basis for peer screens on filed numbers."""
    src = resolve_provider(provider, ("sec",))
    df = sec.frames(tag, period, taxonomy, unit)
    return Result(df.sort_values("val", ascending=False).head(limit), provider=src)


# --------------------------------------------------------------------------- #
# Estimates
# --------------------------------------------------------------------------- #
@command("/equity/estimates/price_target", providers=("yahoo",), summary="Analyst price targets")
def price_target(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    rows = []
    for sym in norm_symbols(symbol):
        targets = yahoo.price_targets(sym)
        rows.append(dict(symbol=sym, **targets))
    return Result(rows, provider=src)


@command("/equity/estimates/consensus", providers=("yahoo",),
         summary="Consensus recommendation and target")
def consensus(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    rows = []
    for sym in norm_symbols(symbol):
        i = yahoo.info(sym)
        rows.append(
            {
                "symbol": sym, "recommendation": i.get("recommendationKey"),
                "recommendation_mean": i.get("recommendationMean"),
                "analyst_count": i.get("numberOfAnalystOpinions"),
                "target_mean": i.get("targetMeanPrice"), "target_median": i.get("targetMedianPrice"),
                "target_high": i.get("targetHighPrice"), "target_low": i.get("targetLowPrice"),
                "current_price": i.get("currentPrice"),
            }
        )
    return Result(rows, provider=src)


@command("/equity/estimates/analyst_estimates", providers=("yahoo",),
         summary="Forward EPS and revenue estimates")
def analyst_estimates(symbol: str, kind: str = "earnings", provider: Optional[str] = None) -> Result:
    """``kind``: earnings, revenue, eps_trend, eps_revisions or growth."""
    src = resolve_provider(provider, ("yahoo",))
    df = yahoo.estimates(one_symbol(symbol), kind)
    df.index.name = "period"
    return Result(df, provider=src, index_name="period")


@command("/equity/estimates/recommendations", providers=("yahoo",),
         summary="Analyst recommendation distribution over time")
def recommendations(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    return Result(yahoo.recommendations(one_symbol(symbol)), provider=src)


@command("/equity/estimates/upgrades_downgrades", providers=("yahoo",),
         summary="Rating changes by firm")
def upgrades_downgrades(symbol: str, limit: int = 100, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = yahoo.upgrades_downgrades(one_symbol(symbol))
    df.index.name = "date"
    return Result(df.head(limit), provider=src, index_name="date")


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #
@command("/equity/ownership/institutional", providers=("yahoo",), summary="Top institutional holders")
def ownership_institutional(symbol: str, limit: int = 50, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = yahoo.holders(one_symbol(symbol), "institutional")
    df.columns = [str(c).strip().lower().replace(" ", "_").replace("%", "pct") for c in df.columns]
    return Result(df.head(limit), provider=src)


@command("/equity/ownership/mutual_fund", providers=("yahoo",), summary="Top mutual-fund holders")
def ownership_mutual_fund(symbol: str, limit: int = 50, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = yahoo.holders(one_symbol(symbol), "mutualfund")
    df.columns = [str(c).strip().lower().replace(" ", "_").replace("%", "pct") for c in df.columns]
    return Result(df.head(limit), provider=src)


@command("/equity/ownership/major_holders", providers=("yahoo",),
         summary="Insider vs institutional ownership split")
def ownership_major(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = yahoo.holders(one_symbol(symbol), "major")
    df.index.name = "metric"
    return Result(df, provider=src, index_name="metric")


@command("/equity/ownership/insider_trading", providers=("yahoo", "sec"),
         summary="Insider buys and sells")
def ownership_insider(symbol: str, limit: int = 100, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo", "sec"))
    sym = one_symbol(symbol)
    if src == "sec":
        return Result(sec.insider_filings(sym, limit), provider=src)
    df = yahoo.insider_transactions(sym)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    return Result(df.head(limit), provider=src)


@command("/equity/ownership/insider_roster", providers=("yahoo",), summary="Registered insiders")
def ownership_insider_roster(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    df = yahoo.holders(one_symbol(symbol), "insider_roster")
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    return Result(df, provider=src)


@command("/equity/ownership/share_statistics", providers=("yahoo",),
         summary="Float, insider and institutional ownership percentages")
def ownership_share_statistics(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    rows = []
    for sym in norm_symbols(symbol):
        i = yahoo.info(sym)
        rows.append(
            {
                "symbol": sym, "shares_outstanding": i.get("sharesOutstanding"),
                "float_shares": i.get("floatShares"),
                "held_percent_insiders": i.get("heldPercentInsiders"),
                "held_percent_institutions": i.get("heldPercentInstitutions"),
                "implied_shares_outstanding": i.get("impliedSharesOutstanding"),
                "shares_short": i.get("sharesShort"), "short_ratio": i.get("shortRatio"),
            }
        )
    return Result(rows, provider=src)


@command("/equity/ownership/form_13f", providers=("sec",), summary="13F/13D/13G filings")
def ownership_13f(symbol: str, limit: int = 40, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("sec",))
    return Result(sec.institutional_filings(one_symbol(symbol), limit), provider=src)
