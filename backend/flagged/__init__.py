"""Change detection: what moved between a filer's last two filings.

Every screen elsewhere in this platform measures a *level* — a P/E, a margin, a
short interest, a twelve-month return. Levels are the commodity part of market
data: the number is on the tape, every vendor sells it, and by the time a screen
can rank on it the ranking is common knowledge. What is not commodity is the
*delta* between two filings by the same filer, because computing one means
holding both documents open and knowing which parts of them are comparable.

That is what this package does.

Twelve flag types, each of them a diff:

* a risk factor that was added, or one that was dropped;
* a customer-concentration disclosure that appeared, or one that vanished;
* an auditor that changed;
* a share count moving the wrong way against money spent on buybacks;
* deferred revenue diverging from the revenue actually recognised;
* receivables growing faster than the sales that produced them;
* an accounting concept the filer had never tagged before;
* a one-sided cluster of sell-side rating changes;
* an institutional entry or exit at a small cap large enough, against the
  name's own trading volume, to be a liquidity event rather than a view;
* several companies disclosing the same end market reporting the same
  inflection in it while one member's consensus has not moved.

Two properties make the set worth having together. First, every one of them is
**dated** — a filing has a filing date, and that date is the first day anyone
outside the company could have known. So a flag drops straight into the graded
signal log (:mod:`backend.thesis.memory`) with an honest ``known_on``, is
measured against its benchmark once the horizon elapses, and earns or fails to
earn a base rate exactly like every other idea source here. Nothing in this
package asserts that a flag predicts anything; the log is what will eventually
say.

Second, the numeric half is computable **for the entire market** without a
vendor. SEC's XBRL frames endpoint answers "every filer's value for this one
concept in this one period" in a single request, so receivables-versus-sales for
all several thousand filers is four requests rather than several thousand — see
:mod:`backend.flagged.market`. The vendor screens this replaces charge for
staler versions of the same arithmetic.

What this package is not: a scoring model. A flag says a thing changed and shows
the two states it changed between, with the form, the date and the URL of the
filing it was read from. Whether the change matters is a question for a reader,
and every flag type below states the way it characteristically produces a false
positive so the reader starts from the objection rather than the headline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: The signal-log namespace every flag is recorded under. The specific flag type
#: becomes the family inside it, which is what the base-rate report splits on —
#: an auditor change and a receivables build are not the same bet and a report
#: that pools them can never say so.
NAMESPACE = "flagged"

# Flag type slugs, declared once so a detector and its catalogue entry cannot
# drift apart.
RISK_FACTOR_ADDED = "risk_factor_added"
RISK_FACTOR_REMOVED = "risk_factor_removed"
CONCENTRATION_APPEARED = "concentration_appeared"
CONCENTRATION_VANISHED = "concentration_vanished"
AUDITOR_CHANGE = "auditor_change"
BUYBACK_SHARE_GAP = "buyback_share_gap"
DEFERRED_REVENUE_DIVERGENCE = "deferred_revenue_divergence"
RECEIVABLES_OUTRUNNING_SALES = "receivables_outrunning_sales"
NEW_ACCOUNTING_CONCEPT = "new_accounting_concept"
RATING_SHIFT = "rating_shift"
INSTITUTIONAL_FLOW = "institutional_flow"
READ_THROUGH = "read_through"

#: How a flag was computed, which decides what it costs and how it fails.
FROM_DOCUMENT = "document"   # two filing documents read as text
FROM_XBRL = "xbrl"           # the filer's own tagged facts
FROM_INDEX = "index"         # the filing index alone (form types, item numbers)
#: The one kind of flag here that is not primary-source. Sell-side ratings are a
#: vendor's record of other people's opinions, with no filing behind them and no
#: obligation on anyone to keep it complete. Kept separate from the three above
#: so a reader can tell at a glance which flags rest on a document and which
#: rest on a feed.
FROM_ESTIMATES = "estimates"
#: The market's aggregate 13F position — SEC's own structured data set of every
#: information table filed in a quarter. Primary-source, but forty-five days
#: late by statute and a further fortnight by publication.
FROM_HOLDINGS = "holdings"
#: Several companies' filings read together: the revenue note's end-market
#: lines from each member's XBRL instance, plus vendor consensus for whether the
#: market has processed what those lines say. Costs a peer group and one filing
#: read per member, so it is on demand rather than part of ``all``.
FROM_CLUSTER = "cluster"


@dataclass(frozen=True)
class FlagType:
    """One kind of change, with the objection to it stated up front."""

    #: URL-safe slug; also the family this flag is logged under.
    name: str
    label: str
    #: What the diff actually measures, in one sentence.
    what: str
    #: Which two states are compared, and what they are read from.
    compares: str
    #: Where it comes from — :data:`FROM_DOCUMENT`, :data:`FROM_XBRL` or
    #: :data:`FROM_INDEX`. Document flags cost two filing downloads; the others
    #: cost one cached JSON object.
    read_from: str
    #: How *this* flag characteristically produces a false positive. Stated for
    #: the same reason every idea source states one: the reader (or the triage
    #: model) should have to argue past the objection rather than meet it later.
    artifact: str
    #: The conventional reading, phrased as the hypothesis it is. Empty where
    #: the flag genuinely does not carry one.
    reading: str = ""

    @property
    def directional(self) -> bool:
        """Does this flag come with a conventional reading at all?"""
        return bool(self.reading)

    def describe(self) -> Dict[str, Any]:
        # The prose is written with RST-style ``literals`` for the Python
        # docs; a caller rendering the catalogue does not want the backticks.
        plain = lambda text: text.replace("``", "")  # noqa: E731
        return {
            "flag": self.name,
            "label": self.label,
            "what": plain(self.what),
            "compares": plain(self.compares),
            "read_from": self.read_from,
            "directional": self.directional,
            "reading": plain(self.reading) or None,
            "artifact": plain(self.artifact),
        }


CATALOGUE: Tuple[FlagType, ...] = (
    FlagType(
        name=RISK_FACTOR_ADDED,
        label="Risk factor added",
        what="A risk-factor paragraph in the newest annual report with no close "
             "match anywhere in the previous one.",
        compares="Item 1A of the two most recent 10-K / 20-F filings, paragraph "
                 "by paragraph.",
        read_from=FROM_DOCUMENT,
        reading="Something the company now thinks it has to warn about that it "
                "did not warn about a year ago.",
        artifact=(
            "Risk factors are written by lawyers on an annual cycle, and most "
            "additions are the cycle rather than the company. A season's "
            "boilerplate arrives across a whole industry at once — cybersecurity, "
            "AI, tariffs, a pandemic — so an addition that every peer also made "
            "is news about the drafting bar, not about this filer. A filer that "
            "reorganises Item 1A produces dozens of apparent additions in one "
            "year. Check whether the same paragraph appeared at the peers before "
            "reading it as specific."
        ),
    ),
    FlagType(
        name=RISK_FACTOR_REMOVED,
        label="Risk factor removed",
        what="A risk-factor paragraph in the previous annual report with no close "
             "match in the newest one.",
        compares="Item 1A of the two most recent 10-K / 20-F filings, paragraph "
                 "by paragraph.",
        read_from=FROM_DOCUMENT,
        reading="A warning the company has stopped giving — either the risk "
                "resolved, or it stopped being worth the words.",
        artifact=(
            "Removal is the rarer and more interesting half, and also the one "
            "this diff most easily invents. Two risk factors merged into one "
            "read as two removals and one addition; a paragraph split in two "
            "reads as one removal and two additions. Where a filing shows "
            "removals and additions of similar length together, the likeliest "
            "explanation is an edit rather than a change of position — the "
            "``rewrite_suspected`` column says when that pattern is present."
        ),
    ),
    FlagType(
        name=CONCENTRATION_APPEARED,
        label="Concentration disclosure appeared",
        what="A customer, supplier or receivable concentration stated in the "
             "newest annual report that the previous one did not state.",
        compares="Concentration sentences (a counterparty or an unnamed "
                 "'one customer', with a percentage) in the two most recent "
                 "annual reports.",
        read_from=FROM_DOCUMENT,
        reading="A counterparty has crossed the disclosure threshold, so the "
                "filer now depends on it enough that US GAAP requires saying so.",
        artifact=(
            "A concentration crosses the 10% line as often because the "
            "denominator shrank as because the counterparty grew — a filer "
            "losing revenue everywhere else discloses a new concentration "
            "without a single new order. The threshold is also a share of "
            "revenue, never of profit, so the disclosed counterparty need not be "
            "the one that matters. And the sentence is prose: a filer restating "
            "the same fact in different words appears here as a disclosure "
            "appearing."
        ),
    ),
    FlagType(
        name=CONCENTRATION_VANISHED,
        label="Concentration disclosure vanished",
        what="A concentration the previous annual report stated that the newest "
             "one does not.",
        compares="Concentration sentences in the two most recent annual reports.",
        read_from=FROM_DOCUMENT,
        reading="A dependency the filer disclosed a year ago is no longer above "
                "the threshold — the customer left, shrank, or the rest of the "
                "business grew past it.",
        artifact=(
            "The three causes are not distinguishable from the absence alone, "
            "and they point in opposite directions: losing the customer is bad, "
            "outgrowing it is good, and the filer choosing to phrase it "
            "differently is nothing. A vanished disclosure is direction-neutral "
            "until the revenue line says which happened, which is why the "
            "revenue change over the same two periods is attached to the row."
        ),
    ),
    FlagType(
        name=AUDITOR_CHANGE,
        label="Auditor change",
        what="The registrant reported a change of certifying accountant, or the "
             "audit firm named on the newest annual report is not the one named "
             "on the previous one.",
        compares="8-K Item 4.01 filings on the filing index, plus the PCAOB firm "
                 "id and auditor name on the two most recent annual report "
                 "cover pages.",
        read_from=FROM_INDEX,
        artifact=(
            "An auditor change is a fact, not a verdict, and most are routine: a "
            "fee tender, mandatory rotation in a non-US jurisdiction, the audit "
            "firm itself merging or exiting a practice area, or a filer "
            "outgrowing a regional practice. What separates those from the "
            "interesting cases is what the 8-K actually says — whether the "
            "accountant resigned or was dismissed, and whether there were "
            "disagreements or reportable events — and the item number alone says "
            "none of it. Read the filing this row links to before concluding "
            "anything. The cover-page reading has its own limit: the PCAOB firm "
            "id has only been tagged since fiscal 2021, and before that the only "
            "evidence is the signature under the audit report, where a firm "
            "restyling its own name reads as a change of auditor."
        ),
    ),
    FlagType(
        name=BUYBACK_SHARE_GAP,
        label="Share count against buybacks",
        what="Cash left the company for share repurchases while the diluted "
             "share count failed to fall by a comparable amount.",
        compares="``PaymentsForRepurchaseOfCommonStock`` against the change in "
                 "``WeightedAverageNumberOfDilutedSharesOutstanding``, over the "
                 "same two reporting periods.",
        read_from=FROM_XBRL,
        reading="The buyback is funding dilution rather than shrinking the "
                "share base — the per-share effect the spending is usually "
                "described as buying did not arrive.",
        artifact=(
            "This is arithmetic about two line items, not a claim of bad faith. "
            "Share-based compensation offsetting a repurchase is the ordinary "
            "state of a software company and is disclosed openly; an "
            "acquisition paid in stock swamps a year of buybacks by design. "
            "Weighted-average counts also lag the repurchase mechanically — "
            "shares bought in the last month of the year barely move the "
            "average — so a single period's gap is weak evidence and the "
            "multi-year cumulative figure on the row is the one worth reading. "
            "An authorisation, finally, is a ceiling and not a commitment."
        ),
    ),
    FlagType(
        name=DEFERRED_REVENUE_DIVERGENCE,
        label="Deferred revenue diverging from recognised revenue",
        what="Revenue recognised grew materially faster than the deferred "
             "balance that funds it, or the reverse.",
        compares="``ContractWithCustomerLiabilityCurrent`` (or the older "
                 "``DeferredRevenueCurrent``) against revenue, year over year.",
        read_from=FROM_XBRL,
        reading="Where recognised revenue outruns the deferred balance, the "
                "period was served out of backlog rather than replenished by "
                "new bookings — a leading indicator that runs ahead of the "
                "revenue line by roughly the length of a contract.",
        artifact=(
            "Deferred revenue is an artifact of billing terms at least as much "
            "as of demand. A shift from annual invoicing to monthly collapses "
            "the balance without losing a customer; a large renewal landing on "
            "either side of the balance-sheet date moves it by a quarter's "
            "worth. Purchase accounting writes an acquired deferred balance "
            "down, so an acquisition manufactures this flag outright. The "
            "concept itself also moved — ASC 606 replaced the deferred-revenue "
            "tags with contract-liability ones — so a filer mid-migration can "
            "show a balance falling to nothing that never fell at all."
        ),
    ),
    FlagType(
        name=RECEIVABLES_OUTRUNNING_SALES,
        label="Receivables outrunning sales",
        what="Accounts receivable grew materially faster than the revenue that "
             "produced them, so days sales outstanding rose.",
        compares="``AccountsReceivableNetCurrent`` against revenue, year over "
                 "year, expressed as the change in days sales outstanding.",
        read_from=FROM_XBRL,
        reading="Sales are being made on terms that were not being offered "
                "before, or are not being collected — the oldest accrual "
                "warning there is, and the one that precedes a revenue "
                "restatement more often than any other single ratio.",
        artifact=(
            "Receivables outrun sales for benign reasons every year. A strong "
            "final month puts sales into the balance without time to collect "
            "them; a mix shift toward slower-paying customers, a geography with "
            "longer terms, or an acquisition consolidated mid-year does the "
            "same. Ending a receivables factoring programme moves the balance "
            "sharply with no change in behaviour whatsoever. The ratio is also "
            "blind to the balance date, so a filer whose year ends the day after "
            "a large shipment looks identical to one with a collections problem."
        ),
    ),
    FlagType(
        name=NEW_ACCOUNTING_CONCEPT,
        label="Accounting concept tagged for the first time",
        what="An XBRL concept the filer has never reported before appears in the "
             "newest filing.",
        compares="The set of concepts in the newest filing against every concept "
                 "the filer has tagged in any earlier one.",
        read_from=FROM_XBRL,
        reading="A company reaches for a concept it has never needed when "
                "something has happened that it has never had to account for — "
                "an impairment, a restructuring, a settlement, a going-concern "
                "paragraph.",
        artifact=(
            "Most first appearances are the taxonomy moving rather than the "
            "company. The FASB publishes a new us-gaap taxonomy every year and "
            "filers migrate tags on their own schedule, so a 'new' concept is "
            "very often the same fact under a new name — the give-away is an "
            "old concept going silent in the same filing. Adopting a standard "
            "(leases, credit losses, contract liabilities) introduces a dozen at "
            "once. This is why the rows carry ``watched``: a curated set of "
            "concepts whose first use means something specific is marked, and "
            "everything else is listed as what it is, a change in tagging."
        ),
    ),
    FlagType(
        name=RATING_SHIFT,
        label="Sell-side ratings moved one way",
        what="Covering desks changed their ratings in one direction, in numbers "
             "large enough to be a share of the coverage rather than one desk.",
        compares="Dated upgrade and downgrade actions inside a rolling window, "
                 "against the number of desks covering the name — with the "
                 "consensus mix now and three months ago carried alongside.",
        read_from=FROM_ESTIMATES,
        artifact=(
            "A rating count counts desks, not facts. Every analyst covering a "
            "name re-rates after the same print, so a cluster of downgrades is "
            "usually one company event counted eleven times while reading on the "
            "row as eleven firms independently changing their minds — and the "
            "consensus follows the price at least as often as it leads it, which "
            "makes ratings far better at confirming a move than predicting one. "
            "The scale is lossy in both directions: a cut from Strong Buy to Buy "
            "and one from Buy to Sell are both recorded as a single downgrade, "
            "and a firm renaming its own tiers (Neutral becoming Sector Weight) "
            "produces changes nobody made. Initiations are new coverage, not a "
            "change of mind, which is why they are counted separately and left "
            "out of the net. Coverage of the actions themselves is a vendor's "
            "and is not complete — this is the one flag in this section with no "
            "filing behind it, and the only one whose underlying record nobody "
            "is obliged to keep. It carries no conventional reading for the same "
            "reason: a wave of downgrades is the setup for a short case and for a "
            "capitulation bottom in equal measure, and the count cannot tell them "
            "apart."
        ),
    ),
    FlagType(
        name=INSTITUTIONAL_FLOW,
        label="Institutional flow against the tape",
        what="A quarter-over-quarter change in reported institutional holdings at "
             "a small cap large enough, measured in days of the name's own average "
             "volume, that the entry or exit was itself a liquidity event.",
        compares="Every 13F filer's position in the CUSIP at the two most recent "
                 "quarter ends (filers present in both, so a manager crossing the "
                 "reporting threshold is not a trade), divided by the quarter's "
                 "average daily volume — with what the net sellers still hold, in "
                 "the same units, as the overhang.",
        read_from=FROM_HOLDINGS,
        reading="At a large cap a holdings change is a sentiment reading; at a "
                "small cap it is the tape. A fund two-thirds of the way out of a "
                "position that trades three hundred thousand shares a day will be "
                "a visible share of the volume until it is done, and the part "
                "still to come is the forecastable part.",
        artifact=(
            "Three ways a large flow is not a decision, in order of how often "
            "they occur. Index reconstitution: when a name enters or leaves the "
            "Russell 2000 in June, every index manager buys or sells it in size "
            "on one day, and passive_share on the row is how much of the gross "
            "flow that was. A change of identity: a merger exchange, a "
            "redomicile or a reverse split retires one CUSIP and issues another, "
            "and every holder appears to exit the old and enter the new — rows "
            "where nearly all holders left and nearly nothing remains are labelled "
            "and excluded, but a partial one can slip through. Shared reporting: "
            "a sub-adviser and its parent can both list the same shares, and a "
            "manager starting or stopping to file for a client is a change of "
            "paperwork, not of ownership; the top_buyers and top_sellers on the "
            "row are what let a reader see one filer's whole book move. Two more "
            "things the number cannot say: the reported change already happened, "
            "during the quarter, so the executed part's price impact is in the "
            "past and only the overhang is ahead; and 13F reports long positions "
            "only, so a short seller covering looks like nothing at all. The "
            "whole read is forty-five days late by statute and a fortnight later "
            "by publication, and the row says which quarter it is."
        ),
    ),
    FlagType(
        name=READ_THROUGH,
        label="Shared end-market read-through",
        what="Several companies that disclose revenue on the same end-market line "
             "— a geography or a product — report the same inflection in it, and "
             "one member with real exposure to that line has a consensus that has "
             "not moved.",
        compares="Each cluster member's year-over-year growth in the shared line "
                 "over its two most recent quarters, read from the revenue notes "
                 "of their filings and normalised so 'Greater China' and 'China' "
                 "are one line; against each member's 90-day drift in next-year "
                 "EPS consensus. The peers' disclosures are the evidence; the "
                 "laggard's print is the catalyst.",
        read_from=FROM_CLUSTER,
        reading="What the peers have already told the market about the end market "
                "is what the laggard will tell it next — and the laggard's "
                "estimates say the market has not made the connection.",
        artifact=(
            "Sharing a line is not sharing an exposure. 'China' at an equipment "
            "maker is fab capex and at a sportswear brand is the consumer, and "
            "the normaliser that lets filers agree on the word cannot tell those "
            "apart — the cluster comes from the peer group, so it is only as "
            "coherent as that is, and the peer list on the row is there to be "
            "edited. Fiscal calendars differ by up to two months inside a "
            "cohort, so 'the same quarter' is approximate and one member may "
            "already have caught the turn the others are still reporting. A "
            "flat consensus can also mean nobody covers the name, or that the "
            "sell side already treats the end market as immaterial there — the "
            "exposure share is on the row for that reason. And an inflection is "
            "a change in year-over-year growth, which a single large order, a "
            "product cycle or last year's easy comparison manufactures without "
            "any change in the end market at all. Say which of these applies "
            "before treating the peers' disclosure as this company's claim."
        ),
    ),
)

BY_NAME: Dict[str, FlagType] = {flag.name: flag for flag in CATALOGUE}


def get(name: str) -> FlagType:
    """Look up a flag type by slug. Raises :class:`KeyError` for an unknown one."""
    return BY_NAME[str(name).strip().lower()]


def names() -> List[str]:
    return [flag.name for flag in CATALOGUE]


def catalogue(read_from: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every flag type, optionally restricted to one way of computing them."""
    return [f.describe() for f in CATALOGUE if not read_from or f.read_from == read_from]


def row(flag: str, symbol: str, known_on: str, summary: str,
        score: float, **fields: Any) -> Dict[str, Any]:
    """The shared shape every detector emits.

    ``known_on`` is the filing date of the *newer* document — the first day the
    change was public — and never the period end, which can precede it by
    months. Grading anchors on it, so a detector that anchored on the period
    instead would credit the flag with a move nobody could have traded.
    """
    return {
        "symbol": str(symbol).upper(),
        "flag": flag,
        "family": flag,
        "known_on": str(known_on)[:10],
        "summary": summary,
        "score": round(float(score), 4),
        **fields,
    }
