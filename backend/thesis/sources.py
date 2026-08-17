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
CROWDED_SHORTS = "crowded_shorts"
PRICE_DISLOCATIONS = "price_dislocations"

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
        """Parse and clamp one value; unparseable input falls back to the default."""
        if raw is None or raw == "":
            return self.default
        try:
            if self.kind == "bool":
                return raw if isinstance(raw, bool) else str(raw).strip().lower() in _TRUE
            if self.kind == "str":
                return str(raw).strip()
            value = int(raw) if self.kind == "int" else float(raw)
        except (TypeError, ValueError):
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


def names() -> List[str]:
    return sorted(SOURCES)


def catalogue() -> List[Dict[str, Any]]:
    """Every registered source, for a caller building a picker."""
    return [SOURCES[n].describe() for n in names()]


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


def _short_detail(row: Mapping[str, Any]) -> List[str]:
    return [
        "  crowding: reported short interest {} · 3m average volume {:,.0f}".format(
            _percent(row.get("short_percent")), row.get("avg_volume") or 0),
        "  trading: price {} · cap {} · provider screen rank {}".format(
            _compact_money(row.get("last_price")), _compact_money(row.get("market_cap")),
            row.get("screen_rank", "?")),
    ]


register(Source(
    name=CROWDED_SHORTS,
    label="Crowded shorts / squeeze risk",
    scope=(
        "Liquid US stocks ranked by Yahoo's latest reported short percentage "
        "of shares outstanding. The same crowding can seed a short case or a "
        "long squeeze/reversal case, so this funnel is direction-neutral."
    ),
    command="/thesis/crowded_shorts",
    artifact_rule=(
        "High short interest does not say why positions exist: they may be "
        "paired hedges, stale reports, arbitrage or a well-understood impairment. "
        "It also says nothing about borrow cost, utilization, days to cover or "
        "the timing of a squeeze. Choose long, short or neutral explicitly and "
        "require a company mechanism plus catalyst; crowding alone supports "
        "neither direction."
    ),
    detail=_short_detail,
    disclaimer=(
        "Short-interest data is reported with a lag and does not include borrow "
        "cost or position motive. Treat it as crowding context only."
    ),
    params={
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
