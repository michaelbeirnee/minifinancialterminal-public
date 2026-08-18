"""The menu of idea sources the funnel can triage. Insider was the first, not the only.

The engine below the funnel — triage, deep dive, the spine, the graded log —
never cared where a candidate came from. The wiring did: one hardcoded command
path, one card format that printed officer counts, one prompt that opened with
"SEC insider-filing data". This module is where that knowledge moves, so adding
the second, fifth and ninth idea source is a registration rather than a surgery.

A source is four things:

``command``
    A registered command that scans and returns candidate rows. It is the
    funnel — every number on the card comes from here, computed
    deterministically, before any model sees it.
``params``
    The tunables the funnel accepts, each with its own clamp. The triage
    endpoint reads these off the query string and ignores everything else, so
    one endpoint serves every source without growing a keyword per scanner.
``detail``
    The source-specific lines of an anomaly card. The frame around them
    (symbol, price context, measured base rate) is shared.
``artifact_rule``
    The way *this* source lies. Insider clusters cluster on the calendar; a
    valuation gap is usually a broken denominator; a disclosure shift is often
    a change of wording. Each source states its own failure mode, and it lands
    in the triage prompt as the rule the model must argue past.

Two obligations a source's scanner carries, because the funnel is the only
part that knows the answers:

1. Rows carry ``symbol``, and should carry ``issuer`` and ``family`` — the
   sub-family this row belongs to, which is what the base-rate report splits
   on. A source with one flavour can omit it.
2. The scanner records its own emissions through
   :func:`backend.thesis.memory.record_events` under this source's
   :attr:`Source.namespace`, anchored on the date the market could first know.
   Recording at the scanner rather than at the endpoint is deliberate: a scan
   run from the CLI or the Python interface must land in the log too.

Command paths are validated against the registry lazily, in :func:`resolve`,
because extensions import this module while they are still registering.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

#: Namespaces, declared here so a scanner and its source cannot drift apart.
INSIDER_CLUSTER = "insider_cluster"
CONGRESS_CLUSTER = "congress_cluster"
UNDERVALUED_LARGE_CAPS = "undervalued_large_caps"
UNDERVALUED_GROWTH = "undervalued_growth"
HIGH_GROWTH = "high_growth"
QUALITY_COMPOUNDERS = "quality_compounders"
CASH_GENERATIVE = "cash_generative"
MARGIN_EXPANSION = "margin_expansion"
BALANCE_SHEET_STRESS = "balance_sheet_stress"
MOMENTUM_LEADERS = "momentum_leaders"
DIVIDEND_GROWERS = "dividend_growers"
ESTIMATE_REVISIONS = "estimate_revisions"
CROWDED_SHORTS = "crowded_shorts"
PRICE_DISLOCATIONS = "price_dislocations"
SECTOR_ROTATION = "sector_rotation"
LINK_PROPAGATION = "link_propagation"
PAIR_DISLOCATION = "pair_dislocation"

STOCK_UNIVERSE = "stocks"
SECTOR_UNIVERSE = "sectors"

_TRUE = {"1", "true", "yes", "on", "y", "t"}


@dataclass(frozen=True)
class Param:
    """One tunable a funnel accepts, with the clamp that keeps it sane.

    Clamping lives with the declaration rather than inside each scanner so the
    endpoint can accept arbitrary query strings from arbitrary sources without
    trusting any of them.
    """

    kind: str  # "int" | "float" | "bool" | "str"
    default: Any = None
    low: Optional[float] = None
    high: Optional[float] = None
    help: str = ""

    def coerce(self, raw: Any) -> Any:
        """Parse and clamp one value; unparseable input falls back to the default.

        Two things this has to get right, because the query string reaching it
        is arbitrary. ``NaN`` parses as a float and then passes every clamp —
        ``max(nan, low)`` and ``min(nan, high)`` are both ``nan`` — so it would
        arrive at a scanner as a bound that nothing can satisfy. And an integer
        param written ``20.0`` is a value the caller plainly meant, not a
        parse failure to be replaced by a default the caller never asked for.
        """
        if raw is None or raw == "":
            return self.default
        try:
            if self.kind == "bool":
                return raw if isinstance(raw, bool) else str(raw).strip().lower() in _TRUE
            if self.kind == "str":
                return str(raw).strip()
            value = float(raw)
        except (TypeError, ValueError):
            return self.default
        if value != value or value in (float("inf"), float("-inf")):
            return self.default
        if self.low is not None:
            value = max(value, self.low)
        if self.high is not None:
            value = min(value, self.high)
        return int(value) if self.kind == "int" else float(value)

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "default": self.default, "min": self.low,
                "max": self.high, "help": self.help}


@dataclass(frozen=True)
class Source:
    """One way of finding candidates worth a human's investigation time."""

    #: URL-safe slug; also the default family namespace in the signal log.
    name: str
    label: str
    #: One sentence for the model: what this funnel looked at to emit these rows.
    scope: str
    #: The registered command that does the scanning.
    command: str
    #: How this source characteristically produces false positives.
    artifact_rule: str
    #: The source-specific lines of an anomaly card.
    detail: Callable[[Mapping[str, Any]], List[str]]
    params: Dict[str, Param] = field(default_factory=dict)
    #: What a caller must be told about these rows regardless of the verdict.
    disclaimer: str = ""
    #: Defaults to ``name``; set only when a scanner already writes another.
    namespace: Optional[str] = None
    #: Shared card enrichments this source's own detail lines already carry.
    #: The congress funnel does not want a "congress" line describing the very
    #: disclosures it selected on — that reads as two populations agreeing when
    #: it is one population counted twice.
    skip_enrichments: Tuple[str, ...] = ()
    #: Which generator tab owns this source. Existing registrations are stocks.
    universe: str = STOCK_UNIVERSE

    @property
    def family_namespace(self) -> str:
        return self.namespace or self.name

    def wants(self, enrichment: str) -> bool:
        """Is this shared card line worth adding for this source?"""
        return enrichment not in self.skip_enrichments

    def resolve_params(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        """Declared params only, coerced and clamped. Unknown keys are dropped.

        Silently ignoring the undeclared is the point: the triage endpoint is
        shared, so a query string left over from another source must not reach
        this scanner as a surprise keyword.
        """
        return {name: spec.coerce(raw.get(name)) for name, spec in self.params.items()}

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "scope": self.scope,
            "command": self.command,
            "namespace": self.family_namespace,
            "universe": self.universe,
            "disclaimer": self.disclaimer,
            "skip_enrichments": list(self.skip_enrichments),
            "params": {k: v.describe() for k, v in self.params.items()},
        }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
SOURCES: Dict[str, Source] = {}

#: The source the triage endpoint runs when the caller names none. A registry
#: decision, not a router literal — the day a better funnel exists, it changes
#: here.
DEFAULT = INSIDER_CLUSTER


def register(source: Source) -> Source:
    if source.name in SOURCES:
        raise ValueError("idea source {!r} is already registered".format(source.name))
    SOURCES[source.name] = source
    return source


def get(name: Optional[str]) -> Source:
    """Look up a source by slug. Raises :class:`KeyError` for an unknown one."""
    slug = str(name or DEFAULT).strip().lower()
    if slug not in SOURCES:
        raise KeyError(slug)
    return SOURCES[slug]


def names(universe: Optional[str] = None) -> List[str]:
    selected = SOURCES
    if universe:
        selected = {name: source for name, source in SOURCES.items()
                    if source.universe == universe}
    return sorted(selected)


def catalogue(universe: Optional[str] = None) -> List[Dict[str, Any]]:
    """Registered sources, optionally restricted to one generator tab."""
    return [SOURCES[n].describe() for n in names(universe)]


def default_for(universe: Optional[str] = None) -> Optional[str]:
    """Default source within a universe, or ``None`` when it has no sources."""
    available = names(universe)
    if not available:
        return None
    return DEFAULT if DEFAULT in available else available[0]


def resolve(source: Source) -> Source:
    """Assert the source's funnel is actually registered. Called at run time.

    Import time is too early: extensions import this module while the registry
    is still filling, so a source declared in one of them would be validated
    against a half-built registry.
    """
    from ..core.registry import REGISTRY

    if source.command not in REGISTRY:
        raise LookupError(
            "idea source {!r} names an unregistered funnel {!r}".format(
                source.name, source.command)
        )
    return source


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def _money(value: Any) -> str:
    return "${:,.0f}".format(value) if isinstance(value, (int, float)) else "$?"


def _compact_money(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "$?"
    if abs(value) >= 1e9:
        return "${:,.1f}B".format(value / 1e9)
    if abs(value) >= 1e6:
        return "${:,.1f}M".format(value / 1e6)
    return _money(value)


def _multiple(value: Any) -> str:
    return "{:.1f}x".format(value) if isinstance(value, (int, float)) else "n/a"


def _percent(value: Any, signed: bool = False) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return ("{:+.1f}%" if signed else "{:.1f}%").format(100 * value)


def _insider_detail(row: Mapping[str, Any]) -> List[str]:
    """The insider funnel's own numbers — nothing the scanner did not compute."""
    return [
        "  cluster: officers={} ({}) · board-backed={}{} · last filing {}".format(
            row.get("officer_buyers", 0), _money(row.get("officer_value", 0)),
            _money(row.get("board_backed_value", 0)),
            " via " + row["board_backed_via"] if row.get("board_backed_via") else "",
            row.get("last_filing", "?")),
        "  buyers: {} · total buyers={} · has_ceo_cfo={}".format(
            row.get("buyers", "?"), row.get("total_buyers", "?"),
            row.get("has_ceo_cfo", False)),
    ]


register(Source(
    name=INSIDER_CLUSTER,
    label="Insider buying clusters",
    scope=(
        "Issuers where several officers or directors bought stock with their own "
        "cash inside one window, or where a 10% holder with board representation "
        "did, read from SEC Form 4 filings."
    ),
    command="/thesis/insider_clusters",
    artifact_rule=(
        "Insiders trade on calendar, not conviction. Clusters just after an "
        "earnings release or in a routine open window are usually artifacts — "
        "say so in calendar_artifact_risk and decline rather than inventing a "
        "story. Signals that are all one family, or all one actor, are not "
        "convergence."
    ),
    detail=_insider_detail,
    disclaimer=(
        "In the platform's 2023-2025 calibration the median insider-cluster event "
        "underperformed its benchmark at every horizon. A cluster is a reason to "
        "investigate, never a reason to buy."
    ),
    params={
        "quarters": Param("int", 2, 1, 8,
                          "Bulk archive quarters to scan (the newest lag ~1 quarter)."),
        "fresh_days": Param("int", 0, 0, 30,
                            "Also sweep this many business days of the EDGAR daily index."),
        "min_officers": Param("int", 2, 1, 10,
                              "Distinct officer/director buyers required to fire the gate."),
        "min_officer_value": Param("float", 1_000_000, 0, 1e9,
                                   "Combined officer purchase value required, in dollars."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _sector_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  sector ETF: {} · 1m {} · 3m {} · YTD {} · 1y {}".format(
            row.get("symbol", "?"),
            _percent(row.get("one_month"), signed=True),
            _percent(row.get("three_month"), signed=True),
            _percent(row.get("ytd"), signed=True),
            _percent(row.get("one_year"), signed=True)),
        "  vs SPY: 1m {} · 3m {} · YTD {} · 1y {}".format(
            _percent(row.get("relative_one_month"), signed=True),
            _percent(row.get("relative_three_month"), signed=True),
            _percent(row.get("relative_ytd"), signed=True),
            _percent(row.get("relative_one_year"), signed=True)),
    ]


register(Source(
    name=SECTOR_ROTATION,
    label="Sector rotation and dislocations",
    scope=(
        "The 11 US GICS sectors represented by their liquid SPDR ETFs, ranked "
        "by the magnitude of three-month performance versus the S&P 500."
    ),
    command="/thesis/sector_rotation",
    artifact_rule=(
        "A sector ETF is a cap-weighted proxy, not the sector's average company. "
        "One mega-cap can create apparent breadth, and recent relative returns "
        "do not reveal whether the driver was rates, commodities, regulation, "
        "earnings revisions or simple factor exposure. Do not extrapolate "
        "leadership or call mean reversion without a causal macro or fundamental "
        "mechanism, a catalyst, and an explicit condition that would refute it."
    ),
    detail=_sector_detail,
    disclaimer=(
        "Sector-ETF performance is a rotation screen, not a recommendation or "
        "evidence that every constituent shares the same setup."
    ),
    skip_enrichments=("concentration", "congress"),
    universe=SECTOR_UNIVERSE,
    params={
        "min_relative_pct": Param(
            "float", 2.0, 0.0, 25.0,
            "Relative move needed to label a sector leader or laggard, in percent."),
        "limit": Param("int", 11, 1, 11, "Maximum sector cards to send to triage."),
    },
))


def _value_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  valuation: trailing P/E {} · forward P/E {} · PEG {} · cap {}".format(
            _multiple(row.get("pe_ratio")), _multiple(row.get("forward_pe")),
            _multiple(row.get("peg_ratio")), _compact_money(row.get("market_cap"))),
        "  growth fields: EPS {} · revenue {} · provider screen rank {}".format(
            _percent(row.get("eps_growth"), signed=True),
            _percent(row.get("revenue_growth"), signed=True),
            row.get("screen_rank", "?")),
    ]


register(Source(
    name=UNDERVALUED_LARGE_CAPS,
    label="Undervalued large caps",
    scope=(
        "US-listed companies with $10B-$100B market values that Yahoo's saved "
        "screen places below 20x trailing earnings and below 1x five-year PEG."
    ),
    command="/thesis/undervalued_large_caps",
    artifact_rule=(
        "A low P/E or PEG is usually a question about the denominator, not an "
        "answer about value. Peak-cycle earnings, leverage, asset intensity, "
        "one-time gains, stale growth estimates and structurally shrinking "
        "businesses all look cheap. Verify the earnings base and say what "
        "expectation is wrong before promoting; 'cheap' alone is not a mechanism."
    ),
    detail=_value_detail,
    disclaimer=(
        "Screen membership is not a valuation conclusion. The provider's "
        "earnings and growth periods must be reconciled to current filings."
    ),
    params={
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


register(Source(
    name=UNDERVALUED_GROWTH,
    label="Undervalued growth",
    scope=(
        "US-listed companies that Yahoo's saved screen places below 20x "
        "trailing earnings and 1x five-year PEG while showing at least 25% "
        "trailing EPS growth."
    ),
    command="/thesis/undervalued_growth",
    artifact_rule=(
        "The valuation and growth fields may describe different periods, and "
        "high trailing growth is often a rebound from an easy comparison or a "
        "one-time margin step. Reconcile the periods, verify revenue and cash "
        "conversion, and identify why the growth persists. A PEG screen does "
        "not establish durable growth or an expectations gap."
    ),
    detail=_value_detail,
    disclaimer=(
        "Growth-at-a-discount is a hypothesis produced by mixed-period provider "
        "fields, not a fact. Verify growth durability and the valuation base."
    ),
    params={
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _growth_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  growth: revenue {} · EPS {} · provider screen rank {}".format(
            _percent(row.get("revenue_growth"), signed=True),
            _percent(row.get("eps_growth"), signed=True),
            row.get("screen_rank", "?")),
        "  expectations context: trailing P/E {} · forward P/E {} · cap {}".format(
            _multiple(row.get("pe_ratio")), _multiple(row.get("forward_pe")),
            _compact_money(row.get("market_cap"))),
    ]


register(Source(
    name=HIGH_GROWTH,
    label="High growth",
    scope=(
        "NYSE- and Nasdaq-listed companies with at least the selected year-over-year "
        "quarterly revenue growth, trailing EPS growth and market-cap floor, ranked "
        "by reported revenue growth."
    ),
    command="/thesis/high_growth",
    artifact_rule=(
        "High reported growth can come from an easy comparison, acquisition, currency, "
        "cyclical rebound, a very small denominator or cost cuts rather than durable "
        "organic demand. It can also be fully priced. Reconcile the periods and base, "
        "verify organic revenue and cash conversion, account for stock-based compensation "
        "and dilution, and name a catalyst plus the evidence that would show deceleration."
    ),
    detail=_growth_detail,
    disclaimer=(
        "This is a high-growth research queue, not proof of durable compounding or an "
        "attractive entry price. Provider growth fields can be stale or mixed-period."
    ),
    params={
        "min_revenue_growth_pct": Param(
            "float", 20.0, 0.0, 200.0,
            "Minimum year-over-year quarterly revenue growth, in percent."),
        "min_eps_growth_pct": Param(
            "float", 20.0, 0.0, 500.0,
            "Minimum trailing EPS growth, in percent."),
        "min_market_cap_bn": Param(
            "float", 2.0, 0.1, 1000.0,
            "Minimum market capitalisation, in USD billions."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _quality_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  returns: ROE {} · gross margin {} · operating margin {}".format(
            _percent(row.get("return_on_equity")), _percent(row.get("gross_margin")),
            _percent(row.get("operating_margin"))),
        "  balance and price: debt/equity {} · revenue growth {} · "
        "trailing P/E {} · forward P/E {}".format(
            _multiple(row.get("debt_to_equity")),
            _percent(row.get("revenue_growth"), signed=True),
            _multiple(row.get("pe_ratio")), _multiple(row.get("forward_pe"))),
    ]


register(Source(
    name=QUALITY_COMPOUNDERS,
    label="Quality compounders",
    scope=(
        "NYSE- and Nasdaq-listed companies clearing a return-on-equity floor and "
        "a gross-margin floor while staying under a debt-to-equity ceiling, ranked "
        "by return on equity."
    ),
    command="/thesis/quality_compounders",
    artifact_rule=(
        "Return on equity is a ratio with a denominator that management "
        "controls. Years of buybacks shrink book equity and can drive it "
        "arbitrarily high or negative; goodwill written off does the same; "
        "leverage raises it by construction, which is what the debt ceiling "
        "here is for and why it is not sufficient. A high gross margin is often "
        "a fact about an industry rather than about a company, and quality "
        "visible on a screen is quality the market has already paid for. Do not "
        "promote on the ratios alone: name the source of the advantage, the "
        "evidence it is still widening, and the price at which it stops "
        "mattering."
    ),
    detail=_quality_detail,
    disclaimer=(
        "Screen membership is not a quality verdict. Trailing returns and "
        "margins can reflect a cycle peak, and none of these fields say whether "
        "the advantage that produced them still holds."
    ),
    params={
        "min_return_on_equity_pct": Param(
            "float", 15.0, 0.0, 200.0,
            "Minimum trailing return on equity, in percent."),
        "min_gross_margin_pct": Param(
            "float", 40.0, 0.0, 100.0, "Minimum trailing gross margin, in percent."),
        "max_debt_to_equity_pct": Param(
            "float", 100.0, 0.0, 1000.0,
            "Maximum total debt as a percent of equity."),
        "min_market_cap_bn": Param(
            "float", 2.0, 0.1, 5000.0,
            "Minimum market capitalisation, in USD billions."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _cash_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  cash: levered FCF {} · FCF yield {} · operating cash flow {}".format(
            _compact_money(row.get("free_cash_flow")),
            _percent(row.get("fcf_yield")),
            _compact_money(row.get("operating_cash_flow"))),
        "  context: net margin {} · revenue growth {} · trailing P/E {} · cap {}".format(
            _percent(row.get("profit_margin")),
            _percent(row.get("revenue_growth"), signed=True),
            _multiple(row.get("pe_ratio")), _compact_money(row.get("market_cap"))),
    ]


register(Source(
    name=CASH_GENERATIVE,
    label="Cash generators (FCF yield)",
    scope=(
        "NYSE- and Nasdaq-listed companies inside the selected size band with "
        "substantial and growing levered free cash flow, ranked by free cash flow "
        "yield — the trailing flow measured against the market value being asked "
        "for it."
    ),
    command="/thesis/cash_generative",
    artifact_rule=(
        "Trailing free cash flow is a single year of a lumpy series. It is "
        "flattered by deferred maintenance capex, a working-capital release, an "
        "asset sale or a customer prepayment, and depressed by exactly the "
        "growth investment that would justify the price. A high yield is "
        "usually the market pricing a decline rather than missing a bargain — "
        "cheap on cash is where cyclicals sit at the top of the cycle and "
        "melting businesses sit permanently. Reconcile operating cash flow to "
        "net income, separate maintenance from growth capex, and say what makes "
        "this flow repeatable before promoting."
    ),
    detail=_cash_detail,
    disclaimer=(
        "Free cash flow yield is computed from the provider's trailing levered "
        "free cash flow and current market value. It excludes net debt, so it is "
        "an equity yield rather than an enterprise one, and a leveraged company "
        "will look cheaper on it than it is."
    ),
    params={
        "min_free_cash_flow_mn": Param(
            "float", 200.0, 0.0, 100_000.0,
            "Minimum trailing levered free cash flow, in USD millions."),
        "min_cash_flow_growth_pct": Param(
            "float", 5.0, -100.0, 500.0,
            "Minimum one-year growth in cash from operations, in percent."),
        "min_market_cap_bn": Param(
            "float", 2.0, 0.1, 5000.0,
            "Minimum market capitalisation, in USD billions."),
        "max_market_cap_bn": Param(
            "float", 100.0, 0.2, 10_000.0,
            "Maximum market capitalisation, in USD billions. Yahoo can only sort "
            "by absolute cash flow, so lowering this is what lets the yield "
            "ranking reach anything below the mega-caps."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _margin_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  margins now: gross {} · operating {} · net {}".format(
            _percent(row.get("gross_margin")), _percent(row.get("operating_margin")),
            _percent(row.get("profit_margin"))),
        "  growth (profile, not the gate): revenue {} · earnings {} · "
        "trailing P/E {} · forward P/E {}".format(
            _percent(row.get("revenue_growth"), signed=True),
            _percent(row.get("eps_growth"), signed=True),
            _multiple(row.get("pe_ratio")), _multiple(row.get("forward_pe"))),
    ]


register(Source(
    name=MARGIN_EXPANSION,
    label="Margin expansion / operating leverage",
    scope=(
        "NYSE- and Nasdaq-listed companies whose one-year EBITDA growth cleared a "
        "floor while revenue growth stayed under a ceiling — profits growing "
        "materially faster than sales — ranked by EBITDA growth."
    ),
    command="/thesis/margin_expansion",
    artifact_rule=(
        "Profits outrunning revenue is arithmetic, not a mechanism. The same "
        "wedge is produced by a one-off cost cut that cannot repeat, a "
        "loss-making division disposed of, a change in what gets capitalised "
        "rather than expensed, a weak prior-year comparison, or a genuine fixed "
        "cost base being covered. Only the last is operating leverage, and only "
        "it continues. Note also that the two gate fields are not returned by "
        "the provider: the card shows current margins and profile growth as the "
        "nearest read, so treat the wedge itself as unverified until it is "
        "confirmed in the filings. Identify which cause applies and what would "
        "show the margin giving back before promoting."
    ),
    detail=_margin_detail,
    disclaimer=(
        "The EBITDA- and revenue-growth figures the gate is applied to are not "
        "returned by the provider and are not shown; the margins on each card "
        "are current levels, not the change that selected the row."
    ),
    params={
        "min_ebitda_growth_pct": Param(
            "float", 25.0, 0.0, 500.0,
            "Minimum one-year EBITDA growth, in percent."),
        "max_revenue_growth_pct": Param(
            "float", 15.0, -50.0, 100.0,
            "Maximum one-year revenue growth, in percent — the other half of the wedge."),
        "min_market_cap_bn": Param(
            "float", 2.0, 0.1, 5000.0,
            "Minimum market capitalisation, in USD billions."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _stress_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  balance sheet: debt/equity {} · current ratio {} · levered FCF {}".format(
            _multiple(row.get("debt_to_equity")), _multiple(row.get("current_ratio")),
            _compact_money(row.get("free_cash_flow"))),
        "  operations and positioning: operating margin {} · revenue growth {} · "
        "short interest {} · 1y price {}".format(
            _percent(row.get("operating_margin")),
            _percent(row.get("revenue_growth"), signed=True),
            _percent(row.get("short_percent")),
            _percent(row.get("one_year_change"), signed=True)),
    ]


register(Source(
    name=BALANCE_SHEET_STRESS,
    label="Balance-sheet stress",
    scope=(
        "NYSE- and Nasdaq-listed companies inside the Altman Z distress zone that "
        "also carry heavy total debt relative to EBITDA, ranked from the weakest "
        "solvency score upward. Direction-neutral."
    ),
    command="/thesis/balance_sheet_stress",
    artifact_rule=(
        "Distress is the most reliably pre-priced condition on this menu: a "
        "screen can see the leverage, and so can everyone else, so 'it is "
        "levered' is not a short thesis. The Altman Z-score was fitted on "
        "public manufacturers and is not meaningful for banks, insurers, REITs, "
        "utilities or asset-light software, where a low score is a category "
        "error rather than a finding. Debt to EBITDA is also blind to what "
        "actually causes defaults — maturity walls, covenant headroom, undrawn "
        "revolvers, refinancing already agreed. Say which side you are on, why "
        "the market has it wrong, and what event settles it."
    ),
    detail=_stress_detail,
    disclaimer=(
        "Solvency scores and leverage ratios are screening heuristics computed "
        "from trailing statements, not credit analysis. They say nothing about "
        "maturity schedules, covenants or committed facilities, and a low score "
        "is routine in several sectors the model was never fitted on."
    ),
    params={
        "max_altman_z": Param(
            "float", 1.8, -10.0, 10.0,
            "Altman Z-score ceiling; below ~1.8 is the conventional distress zone."),
        "min_debt_to_ebitda": Param(
            "float", 4.0, 0.0, 50.0,
            "Minimum total debt to EBITDA."),
        "min_market_cap_bn": Param(
            "float", 1.0, 0.05, 1000.0,
            "Minimum market capitalisation, in USD billions."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _momentum_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  run: 1y {} · {} from 52-week high · {} from 52-week low".format(
            _percent(row.get("one_year_change"), signed=True),
            _percent(row.get("high52_dist"), signed=True),
            _percent(row.get("low52_dist"), signed=True)),
        "  trend and context: vs 50d MA {} · vs 200d MA {} · forward P/E {} · "
        "revenue growth {}".format(
            _percent(row.get("ma50_dist"), signed=True),
            _percent(row.get("ma200_dist"), signed=True),
            _multiple(row.get("forward_pe")),
            _percent(row.get("revenue_growth"), signed=True)),
    ]


register(Source(
    name=MOMENTUM_LEADERS,
    label="Momentum leaders near highs",
    scope=(
        "Liquid NYSE- and Nasdaq-listed companies up by at least the selected "
        "amount over twelve months and still trading close to their 52-week "
        "high, ranked by the size of the run and proximity to that high."
    ),
    command="/thesis/momentum_leaders",
    artifact_rule=(
        "This screen selects on an outcome. Every row is here because the move "
        "already happened, which guarantees the sample is the winners and tells "
        "you nothing about the population they were drawn from. The cause is "
        "invisible to the screen and matters entirely: earnings revisions that "
        "have further to run are a thesis, a multiple re-rating on unchanged "
        "earnings is a borrowed return, and index inclusion or a factor flow is "
        "neither. Never write 'strength begets strength' — establish what "
        "changed in the business, whether estimates followed the price or led "
        "it, and the condition that would mark the end of it."
    ),
    detail=_momentum_detail,
    disclaimer=(
        "Momentum screens are outcome-selected and crowded by construction. "
        "Proximity to a 52-week high is a fact about price history, not evidence "
        "of an expectations gap."
    ),
    params={
        "min_year_gain_pct": Param(
            "float", 30.0, 0.0, 1000.0,
            "Minimum twelve-month price gain, in percent."),
        "max_high_distance_pct": Param(
            "float", 15.0, 0.0, 100.0,
            "How far below the 52-week high a candidate may still trade, in percent."),
        "min_avg_volume": Param(
            "float", 500_000, 0.0, 50_000_000,
            "Minimum three-month average daily share volume."),
        "min_market_cap_bn": Param(
            "float", 2.0, 0.1, 5000.0,
            "Minimum market capitalisation, in USD billions."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _income_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  payout: dividend yield {} · payout ratio {} · levered FCF {}".format(
            _percent(row.get("dividend_yield")), _percent(row.get("payout_ratio")),
            _compact_money(row.get("free_cash_flow"))),
        "  business and price: revenue growth {} · earnings growth {} · "
        "trailing P/E {} · 1y price {}".format(
            _percent(row.get("revenue_growth"), signed=True),
            _percent(row.get("eps_growth"), signed=True),
            _multiple(row.get("pe_ratio")),
            _percent(row.get("one_year_change"), signed=True)),
    ]


register(Source(
    name=DIVIDEND_GROWERS,
    label="Dividend growth records",
    scope=(
        "NYSE- and Nasdaq-listed companies with an unbroken dividend-growth streak "
        "of at least the selected length that still offer at least the selected "
        "forward yield, ranked by yield adjusted for payout coverage."
    ),
    command="/thesis/dividend_growers",
    artifact_rule=(
        "The two gates are in tension and the tension is the whole point. A "
        "long streak is evidence of intent, but a yield that has become large "
        "usually means the price fell, and the screen cannot tell a durable "
        "payer trading cheaply from one whose decline the dividend has not "
        "caught up with yet. Streaks are also defended past the point of sense: "
        "management will borrow, sell assets or under-invest to protect one, so "
        "the streak's continuation is weak evidence about the business. Check "
        "coverage from free cash flow rather than from earnings, check whether "
        "the growth rate has quietly fallen to a token raise, and state what "
        "would force a cut."
    ),
    detail=_income_detail,
    disclaimer=(
        "A dividend-growth record describes past distributions only. The streak "
        "length that selected these rows is not returned by the provider and is "
        "not shown on the card; yield and payout coverage are."
    ),
    params={
        "min_growth_years": Param(
            "float", 10.0, 1.0, 60.0,
            "Consecutive years of dividend growth required."),
        "min_forward_yield_pct": Param(
            "float", 2.0, 0.0, 20.0, "Minimum forward dividend yield, in percent."),
        "min_market_cap_bn": Param(
            "float", 2.0, 0.1, 5000.0,
            "Minimum market capitalisation, in USD billions."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _revision_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  revisions (30d, next fiscal year): {} up · {} down · net {} across {} "
        "analysts ({} of coverage)".format(
            row.get("up_30d", "?"), row.get("down_30d", "?"),
            row.get("net_revisions", "?"), row.get("analyst_count", "?"),
            _percent(row.get("revision_breadth"), signed=True)),
        "  consensus EPS {}: {} over 30d · {} over 90d · price 1y {} · "
        "{} from 52-week high".format(
            "{:.2f}".format(row["consensus_eps_fy1"])
            if isinstance(row.get("consensus_eps_fy1"), (int, float)) else "n/a",
            _percent(row.get("eps_drift_30d"), signed=True),
            _percent(row.get("eps_drift_90d"), signed=True),
            _percent(row.get("one_year_change"), signed=True),
            _percent(row.get("high52_dist"), signed=True)),
    ]


register(Source(
    name=ESTIMATE_REVISIONS,
    label="Estimate revisions",
    scope=(
        "Liquid NYSE- and Nasdaq-listed companies whose next-fiscal-year EPS "
        "consensus has moved over ninety days and whose thirty-day analyst "
        "revision count is one-sided, ranked by the size of the move and the "
        "share of coverage behind it. Direction-neutral."
    ),
    command="/thesis/estimate_revisions",
    artifact_rule=(
        "A revision count counts desks, not facts. Every analyst covering a "
        "name re-bases after the same print, so thirty upward revisions is "
        "usually one company event counted thirty times, and it reads on the "
        "card as though the sell side independently changed its mind. The "
        "consensus also follows the price at least as often as it leads it, so "
        "revisions confirm a move far more reliably than they predict one, and "
        "a large drift on thin coverage is one or two desks. Say what actually "
        "changed at the company, whether the price already moved with it, and "
        "what a further revision would have to show to be informative — "
        "'estimates are going up' is a description, not a thesis."
    ),
    detail=_revision_detail,
    disclaimer=(
        "Revision counts and consensus history are the provider's, covering "
        "only sell-side desks it tracks. Estimates are frequently revised "
        "toward the price rather than ahead of it."
    ),
    params={
        "min_net_revisions": Param(
            "int", 3, 1, 50,
            "Minimum one-sided 30-day revision count (up minus down, either way)."),
        "min_estimate_drift_pct": Param(
            "float", 2.0, 0.0, 100.0,
            "Minimum absolute 90-day move in next-year consensus EPS, in percent."),
        "min_analysts": Param(
            "int", 4, 1, 60,
            "Minimum covering analysts; below this one desk moves the consensus."),
        "min_market_cap_bn": Param(
            "float", 2.0, 0.1, 5000.0,
            "Minimum market capitalisation, in USD billions."),
        "min_avg_volume": Param(
            "float", 500_000, 0.0, 50_000_000,
            "Minimum three-month average daily share volume."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _short_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  crowding: short interest {} of float · {} days to cover · "
        "3m average volume {:,.0f}".format(
            _percent(row.get("short_percent")),
            "{:.1f}".format(row["days_to_cover"])
            if isinstance(row.get("days_to_cover"), (int, float)) else "n/a",
            row.get("avg_volume") or 0),
        "  trading: price {} · cap {} · provider screen rank {}".format(
            _compact_money(row.get("last_price")), _compact_money(row.get("market_cap")),
            row.get("screen_rank", "?")),
    ]


register(Source(
    name=CROWDED_SHORTS,
    label="Crowded shorts / squeeze risk",
    scope=(
        "Liquid US listings whose reported short interest is a large share of "
        "float and would take several days of normal volume to cover. The same "
        "crowding can seed a short case or a long squeeze/reversal case, so "
        "this funnel is direction-neutral."
    ),
    command="/thesis/crowded_shorts",
    artifact_rule=(
        "High short interest does not say why positions exist: they may be "
        "paired hedges, stale reports, arbitrage or a well-understood impairment. "
        "It also says nothing about borrow cost, utilization or the timing of a "
        "squeeze, and days to cover is computed from average volume that a "
        "squeeze itself would destroy. Choose long, short or neutral explicitly "
        "and require a company mechanism plus catalyst; crowding alone supports "
        "neither direction."
    ),
    detail=_short_detail,
    disclaimer=(
        "Short-interest data is reported on a settlement-date lag of up to two "
        "weeks and does not include borrow cost or position motive. Treat it as "
        "crowding context only."
    ),
    params={
        "min_short_percent": Param(
            "float", 15.0, 1.0, 90.0,
            "Reported short interest as a percent of float required to qualify."),
        "min_days_to_cover": Param(
            "float", 3.0, 0.0, 30.0,
            "Days of average volume needed to cover the reported short position."),
        "min_avg_volume": Param(
            "float", 500_000, 0.0, 50_000_000,
            "Minimum three-month average daily share volume."),
        "min_market_cap_bn": Param(
            "float", 0.5, 0.05, 1000.0,
            "Minimum market capitalisation, in USD billions."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _dislocation_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  dislocation: 1m {} · 3m {} · RSI14 {}".format(
            _percent(row.get("one_month"), signed=True),
            _percent(row.get("three_month"), signed=True),
            "{:.1f}".format(row["rsi14"])
            if isinstance(row.get("rsi14"), (int, float)) else "n/a"),
        "  trend: vs 50d MA {} · vs 200d MA {} · cap {}".format(
            _percent(row.get("ma50_dist"), signed=True),
            _percent(row.get("ma200_dist"), signed=True),
            _compact_money(row.get("market_cap"))),
    ]


register(Source(
    name=PRICE_DISLOCATIONS,
    label="One-month price dislocations",
    scope=(
        "Index constituents whose adjusted price fell by at least the selected "
        "amount over one month, ranked from the largest drawdown."
    ),
    command="/thesis/price_dislocations",
    artifact_rule=(
        "The screen observes the move, not its cause. A drawdown can be a market "
        "or sector factor, an index rebalance, a temporary event, or the correct "
        "repricing of impaired fundamentals. Separate company-specific residual "
        "move from common-factor exposure and do not assume either a rebound or "
        "continued decline without a catalyst and falsifier."
    ),
    detail=_dislocation_detail,
    disclaimer=(
        "A large drawdown is direction-neutral. It is a queue of moves to explain, "
        "not a mean-reversion or momentum signal."
    ),
    params={
        "index": Param("str", "sp500", None, None,
                       "Universe: sp500, nasdaq100, dowjones, sp400, sp600 or russell1000."),
        "min_drop_pct": Param("float", 12.0, 1.0, 80.0,
                              "Minimum absolute one-month decline, in percent."),
        "mcap_min": Param("float", 2.0, 0.0, 10_000.0,
                          "Minimum market capitalization, in $ billions."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _clip(text: Any, width: int = 220) -> str:
    """One filed sentence, short enough for a card and never silently cut short."""
    body = " ".join(str(text or "").split())
    return body if len(body) <= width else body[: width - 1] + "…"


def _propagation_detail(row: Mapping[str, Any]) -> List[str]:
    """The disclosed link, what moved at the far end of it, and what has not moved here.

    Five lines rather than the usual two, and the last one is a quotation. This
    funnel's entire claim is a sentence in somebody's annual report: without it
    on the card the model is being asked to trust that a relationship exists,
    which is exactly the thing no screener can establish and this one can.
    """
    also = row.get("also_exposed_to")
    segment = row.get("hub_segment")
    lines = [
        "  link: {} of {} comes from {} — {} exposure · {} {}{}".format(
            "{:.1f}%".format(row["exposure_pct"])
            if isinstance(row.get("exposure_pct"), (int, float)) else "n/a",
            row.get("exposure_basis") or "revenue", row.get("hub", "?"),
            row.get("link") or "?", row.get("form") or "?",
            row.get("filing_date") or "?",
            " · also disclosed against {}".format(also) if also else ""),
        "  hub {}: moved {} via {}{} · next-FY consensus {} over 90d · price 3m {}".format(
            row.get("hub", "?"), row.get("hub_direction") or "?",
            row.get("hub_channels") or "?",
            " (channels disagree)" if row.get("hub_conflicting") else "",
            _percent(row.get("hub_eps_drift_90d"), signed=True),
            _percent(row.get("hub_three_month"), signed=True)),
    ]
    if segment:
        lines.append(
            "  hub segment: {} {} y/y after {} ({} of hub revenue, {})".format(
                segment, _percent(row.get("hub_segment_yoy"), signed=True),
                _percent(row.get("hub_segment_yoy_prior"), signed=True),
                _percent(row.get("hub_segment_share")),
                row.get("hub_segment_trend") or "?"))
    lines.append(
        "  here: consensus {} over 90d · net {} revisions across {} analysts · "
        "price 3m {} · cap {}".format(
            _percent(row.get("eps_drift_90d"), signed=True),
            row.get("net_revisions") if row.get("net_revisions") is not None else "?",
            row.get("analyst_count") if row.get("analyst_count") is not None else "no",
            _percent(row.get("three_month"), signed=True),
            _compact_money(row.get("market_cap"))))
    lines.append('  disclosed: "{}"'.format(_clip(row.get("quote"))))
    return lines


register(Source(
    name=LINK_PROPAGATION,
    label="Propagation along disclosed links",
    scope=(
        "Companies whose own annual report puts a percentage on their dependence "
        "on another company where something material has just moved — the hub's "
        "next-year consensus, a reportable segment shrinking or decelerating two "
        "quarters running, or a large price move. The link is a sentence in an "
        "SEC filing, quoted on the card; the percentage is a share of the "
        "candidate's own books, disclosed by the candidate. Direction-neutral: "
        "the same edge carries acceleration at the hub as readily as contraction."
    ),
    command="/thesis/link_propagation",
    artifact_rule=(
        "The edge is real and the inference across it is not. Five ways this "
        "funnel is wrong. (1) The disclosure is as old as the filing it came "
        "from — a concentration stated a year ago may already have ended, which "
        "would be the actual news and is invisible here; check filing_date and "
        "re-read the current filing. (2) The percentage is of the counterparty's "
        "whole company while the shock is of one hub segment, and nothing in "
        "either filing says the counterparty serves that segment. That join is "
        "an assumption — make it explicitly or decline. (3) 'Estimates have not "
        "moved' has three causes and only one of them is an opportunity: nobody "
        "is looking, everybody looked and judged it immaterial, or the "
        "counterparty has already re-sourced the revenue. Say which you are "
        "claiming. (4) A hub's consensus is the sell side's view, not its order "
        "book, and it follows price as often as it leads it; the segment channel "
        "is the only one reading the business itself, so a row whose only "
        "channel is 'consensus' or 'price' is weaker than one carrying "
        "'segment'. (5) Coverage stops at SEC filers who crossed a disclosure "
        "threshold, so the absence of a link is never evidence of "
        "diversification, and the largest exposed company is often private. Do "
        "not treat a percentage as a forecast of anything: a supplier can lose "
        "its largest customer and still beat, and one that is 30% exposed to a "
        "hub growing 40% elsewhere is not short."
    ),
    detail=_propagation_detail,
    # The shared "concentration" line reads the candidate's own annual report
    # for the counterparty it names — which, for a row selected on exactly that
    # sentence, is the same sentence arriving a second time under a different
    # heading. One population counted twice reads as two sources agreeing.
    skip_enrichments=("concentration",),
    disclaimer=(
        "A disclosed concentration is a relationship as it stood on the filing "
        "date, not a live exposure, and the transmission from a hub's segment to "
        "a counterparty's revenue is an inference this platform does not make "
        "for you. Rows whose estimates have already moved are emitted alongside "
        "those whose have not, because the first group is what the second is "
        "measured against."
    ),
    params={
        "hubs": Param("str", "", None, None,
                      "Comma-separated hub symbols to walk. Leave empty to find "
                      "hubs by scanning the largest US listings for a consensus move."),
        "hub_universe": Param("int", 60, 10, 200,
                              "How many of the largest US listings to read when "
                              "discovering hubs. Two estimate requests each."),
        "max_hubs": Param("int", 3, 1, 8,
                          "How many hubs to actually walk. Each walk is a full-text "
                          "search of EDGAR plus the filings that answered."),
        "min_hub_drift_pct": Param(
            "float", 3.0, 0.0, 50.0,
            "How far the hub's next-year EPS consensus must have moved over "
            "ninety days, in percent. Zero walks any named hub."),
        "min_exposure_pct": Param(
            "float", 10.0, 1.0, 100.0,
            "Disclosed dependence required of a counterparty, in percent of its "
            "own revenue, sales, purchases or receivables."),
        "min_market_cap_bn": Param(
            "float", 0.3, 0.0, 1000.0,
            "Minimum counterparty market capitalisation, in USD billions. "
            "Counterparties whose profile cannot be read keep their slot."),
        "read_segments": Param(
            "bool", True, None, None,
            "Read each hub's quarterly segment revenue from its filings — the "
            "only channel that measures demand rather than expectations, and the "
            "slow one on a cold cache."),
        "years": Param("int", 4, 1, 10,
                       "How far back to look for a filing that discloses the link."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))



def _pair_detail(row: Mapping[str, Any]) -> List[str]:
    """The pair, the link that admitted it, the spread against its history, and the evidence.

    Four lines, and the last is the sentence or classification that justified
    testing the pair at all. Without it the model is being asked to trust that
    a cointegration result means something, which is exactly the claim a
    restricted search space exists to make checkable.
    """
    z = row.get("z_now")
    half = row.get("half_life_days")
    lines = [
        "  pair: {} vs {} · {}{} · {}".format(
            row.get("symbol", "?"), row.get("pair_with", "?"),
            str(row.get("relationship") or "?").replace("_", " "),
            " ({} of {} {})".format(
                "{:.1f}%".format(row["exposure_pct"]), row.get("pct_of") or "?",
                row.get("exposure_basis") or "revenue")
            if isinstance(row.get("exposure_pct"), (int, float))
            else (" ({})".format(row["peer_evidence"]) if row.get("peer_evidence") else ""),
            "{} {}".format(row.get("form") or "filing", row.get("filing_date") or "?")
            if row.get("filing_date") else "classification"),
        "  spread: {}σ now · {}σ mean over {}d · {} of {}d outside · {} · "
        "hedge {} · half-life {}".format(
            "{:+.1f}".format(z) if isinstance(z, (int, float)) else "n/a",
            "{:+.1f}".format(row["z_recent_mean"])
            if isinstance(row.get("z_recent_mean"), (int, float)) else "n/a",
            row.get("recent_days") or "?", row.get("days_outside", "?"),
            row.get("recent_days") or "?", row.get("state") or "?",
            "{:.2f}".format(row["hedge_ratio"])
            if isinstance(row.get("hedge_ratio"), (int, float)) else "n/a",
            "{:.0f}d".format(half) if isinstance(half, (int, float)) else "none"),
        "  fit: Engle-Granger p {} on history · {} over full window · return corr {} · "
        "{} obs · rich {} / cheap {} · {} moved {} · here {} · cap {}".format(
            "{:.3f}".format(row["p_value_history"])
            if isinstance(row.get("p_value_history"), (int, float)) else "n/a",
            "{:.3f}".format(row["p_value_full"])
            if isinstance(row.get("p_value_full"), (int, float)) else "n/a",
            "{:.2f}".format(row["return_correlation"])
            if isinstance(row.get("return_correlation"), (int, float)) else "n/a",
            row.get("observations", "?"), row.get("rich_leg", "?"), row.get("cheap_leg", "?"),
            row.get("mover", "?"), _percent(row.get("mover_move"), signed=True),
            _percent(row.get("recent_move"), signed=True),
            _compact_money(row.get("market_cap"))),
        '  linked by: "{}"'.format(_clip(row.get("evidence"))),
    ]
    return lines


register(Source(
    name=PAIR_DISLOCATION,
    label="Pair dislocations along disclosed links",
    scope=(
        "Pairs of companies joined by a disclosed relationship — one names the "
        "other as a supplier, customer or competitor in an SEC filing, or two "
        "independent classifications place them in the same segment — whose "
        "log-price spread, fitted and tested for cointegration on history alone, "
        "now sits at least the selected number of historical sigmas from that "
        "relationship. Only such pairs are tested; no pair without a mechanism "
        "is. The candidate is the leg that has moved less over the recent "
        "window; the pair is named on the card. Direction-neutral."
    ),
    command="/thesis/pair_dislocation",
    artifact_rule=(
        "The relationship is disclosed and the statistics are still statistics. "
        "Five ways this funnel is wrong. (1) Restricting the search to linked "
        "pairs cuts the false-positive count, it does not remove it: at the "
        "p-value ceiling, one in ten unrelated pairs still passes, and the "
        "result reports how many pairs were tested — read the p-value on the "
        "card as one draw among that many. (2) 'Broken' and 'dislocated' are "
        "both descriptions of the spread, not of the business: a spread that "
        "no longer cointegrates is what a relationship that has genuinely ended "
        "looks like, and also what a large lag looks like, and nothing here "
        "tells them apart — the filing might, and it is linked. (3) The link is "
        "as old as its filing; a supplier disclosed at 27% two years ago may "
        "have been designed out, which would explain the spread rather than "
        "contradict it. (4) The hedge ratio and sigma are fitted on the "
        "history and the history contains whatever regime it contains; a "
        "half-life longer than the recent window says the spread never "
        "reverted quickly, so a reading of two sigmas is not a claim it will "
        "close in a quarter. (5) The candidate is the leg that moved less, "
        "which is a convention and not a finding: the leg that moved may be the "
        "one that is wrong. Do not treat a z-score as a forecast; a stretched "
        "spread between a customer and a supplier is either the supplier "
        "lagging or the market having priced the loss of the customer, and "
        "which it is lives in the filings, not the spread."
    ),
    detail=_pair_detail,
    # The shared "concentration" line reads the candidate's annual report for
    # counterparties it names — for a supplier/customer pair, that is the same
    # sentence that admitted the pair, arriving a second time.
    skip_enrichments=("concentration",),
    disclaimer=(
        "A dislocated or broken spread is a queue of pairs to explain, not a "
        "mean-reversion signal. The relationship that justified the pair is a "
        "filing as old as its date, and the fitted spread is a description of "
        "the past that the recent window has departed from — for a reason this "
        "platform does not know."
    ),
    params={
        "symbols": Param("str", "", None, None,
                         "Comma-separated anchor symbols to draw pairs around. Leave "
                         "empty to anchor on the largest US listings."),
        "anchor_universe": Param("int", 40, 5, 200,
                                 "How many of the largest US listings to consider as "
                                 "anchors when none are named."),
        "max_anchors": Param("int", 4, 1, 10,
                             "How many anchors to draw pairs around. Each is a full-text "
                             "search of EDGAR plus the filings that answered."),
        "relationships": Param("str", "supplier,customer,shared_segment", None, None,
                               "Which disclosed links admit a pair: any of supplier, "
                               "customer, shared_segment, comma-separated."),
        "min_exposure_pct": Param(
            "float", 0.0, 0.0, 100.0,
            "Disclosed concentration a supplier or customer link must carry, in "
            "percent of the disclosing side's books. Zero admits any quantified link."),
        "peer_evidence": Param(
            "str", "agree", None, None,
            "How a shared segment must be evidenced: filings (named as competition "
            "in a 10-K), agree (that, or two independent classifications concurring), "
            "or any."),
        "peers": Param("int", 8, 0, 20,
                       "How many comparables per anchor to read for shared-segment "
                       "pairs. Zero turns the leg off."),
        "min_anchor_market_cap_bn": Param(
            "float", 10.0, 0.1, 5000.0,
            "Minimum size of a discovered anchor, in USD billions."),
        "lookback_years": Param("int", 3, 1, 10,
                                "Years of daily prices to fit and test the relationship on."),
        "recent_days": Param("int", 63, 10, 250,
                             "Trading days held out of the fit and read against it. The "
                             "z-score is the last of these."),
        "min_obs": Param("int", 250, 120, 2500,
                         "Overlapping trading days a pair needs before it is tested at all."),
        "z_threshold": Param("float", 2.0, 0.5, 6.0,
                             "How far, in historical sigmas, the spread must sit from the "
                             "fitted relationship to be flagged."),
        "max_p_value": Param("float", 0.10, 0.01, 1.0,
                             "Engle-Granger p-value a pair must clear over its history to "
                             "have had a relationship; the same ceiling over the whole "
                             "window separates dislocated from broken."),
        "include_intact": Param("bool", False, None, None,
                                "Also emit tested pairs whose spread is within the "
                                "threshold, so the whole tested set is visible."),
        "years": Param("int", 4, 1, 10,
                       "How far back to look for a filing that discloses the link."),
        "limit": Param("int", 20, 1, 40, "Maximum candidate cards to send to triage."),
    },
))


def _congress_detail(row: Mapping[str, Any]) -> List[str]:
    """The disclosure funnel's own numbers. Brackets are labelled as brackets."""
    return [
        "  cluster: {} members · {} disclosures · {} self-directed · direction={}".format(
            row.get("members", 0), row.get("disclosures", 0),
            row.get("self_directed", 0), row.get("side", "?")),
        "  disclosed: >= {} combined (bracket floors) · earliest trade {} · "
        "typical filing lag {} days (deadline is 45)".format(
            _money(row.get("amount_floor", 0)), row.get("earliest_trade", "?"),
            row.get("disclosure_lag_days", "?")),
        "  members: {}".format(row.get("member_names", "?")),
    ]


register(Source(
    name=CONGRESS_CLUSTER,
    label="Congressional disclosure clusters",
    scope=(
        "Symbols where several different members of the US Senate disclosed "
        "trades in the same direction inside one window, read from STOCK Act "
        "periodic transaction reports. Senate only — the House publishes PDFs "
        "this platform does not parse — so this sees 100 of 535 members."
    ),
    command="/thesis/congress_clusters",
    artifact_rule=(
        "A disclosure is not a decision. Members must file within 45 days, so "
        "a 'cluster' is often several unrelated trades whose deadlines fell in "
        "the same week — check disclosure_lag_days before reading intent into "
        "the timing. Trades in a spouse's or a managed account were very "
        "possibly never directed by the member: where self_directed is a small "
        "share of disclosures, say so and decline. Index funds and broad ETFs "
        "appearing across many members are asset allocation, not a view on a "
        "company. Amounts are brackets, and amount_floor is a lower bound "
        "rather than a size — never restate it as the money involved."
    ),
    detail=_congress_detail,
    skip_enrichments=("congress",),
    disclaimer=(
        "Congressional disclosures are an attention signal with a legal "
        "deadline attached, not evidence of informed trading. The filing lags "
        "the trade by up to 45 days, so the platform anchors every measurement "
        "on the filing date — the first day anyone outside the household could "
        "have acted."
    ),
    params={
        "min_members": Param("int", 2, 1, 10,
                             "Distinct members required to fire the gate."),
        "window_days": Param("int", 45, 7, 180,
                             "Filing-date window the cluster must form inside."),
        "days": Param("int", 120, 7, 730, "How far back to read filings."),
        "self_directed_only": Param("bool", False, None, None,
                                    "Drop spouse and dependent-child accounts first."),
        "reports": Param("int", 200, 1, 400,
                         "Maximum filings opened; each is one request."),
        "limit": Param("int", 40, 1, 100, "Maximum candidate cards to send to triage."),
    },
))
