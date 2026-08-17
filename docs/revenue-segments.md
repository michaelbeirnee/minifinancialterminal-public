# Revenue segments from filing XBRL

Status: implemented 2026-08-16. `backend/providers/segments.py`,
`/equity/fundamental/revenue_segments`, the Segments tab on the stock page's
FINANCIALS mode, tested in `tests/test_segments.py`.

Every other fundamental command here reads the SEC's **company-facts** API,
which publishes one number per concept per period. That is the whole problem: a
segment breakdown *is* the dimensions, and company-facts strips them off. Ask it
for Apple's revenue and you get $416bn — one number, no iPhone, no Americas, no
Services. There is no free vendor that fills the gap either.

The gap is only in the API. The filing itself carries the same revenue concept a
second time, tagged against a context that names an axis and a member:

```xml
<us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax
    contextRef="c-42" unitRef="usd">209586000000</...>

<xbrli:context id="c-42">
  <xbrli:entity><xbrli:segment>
    <xbrldi:explicitMember dimension="srt:ProductOrServiceAxis"
      >aapl:IPhoneMember</xbrldi:explicitMember>
  </xbrli:segment></xbrli:entity>
  <xbrli:period><xbrli:startDate>2024-09-29</xbrli:startDate>
                <xbrli:endDate>2025-09-27</xbrli:endDate></xbrli:period>
</xbrli:context>
```

So the feature is: fetch the instance document out of the filing folder, read
the contexts, and keep the revenue facts that sit on exactly one breakdown axis.
Everything below is what stands between that and a table you can trust.

## The three axes

A filer may use any or all of them, and most large caps use two:

| Breakdown | Axis (local name) | What it is |
|---|---|---|
| `business` | `StatementBusinessSegmentsAxis` | ASC 280 reportable segments |
| `geographic` | `StatementGeographicalAxis` | revenue by country or region |
| `product` | `ProductOrServiceAxis` | product and service lines |

Matching is on the lower-cased local name, so one rule survives the prefix
moving between taxonomies (`us-gaap` → `srt` in 2021) and the IFRS spellings a
20-F filer uses. Geography is matched before the catch-all "segment" rule, which
would otherwise claim `StatementGeographicalSegmentAxis`.

## What is thrown away, and why

**Cross-tab cells.** Exxon tags revenue on segment × geography together. Adding
those cells to the single-axis rows would count the same revenue twice, so a
fact carrying two breakdown axes is dropped. The consequence is honest and
visible: a filer that *only* cross-tabs reports no segments here at all.

**Reconciling items.** Segment revenue is routinely tagged on the segments axis
*and* on `ConsolidationItemsAxis`. Only `OperatingSegmentsMember` (and its
reportable-segment siblings) survive; an intersegment elimination or a
material-reconciling-item is not a segment.

**Anything on an axis this does not read.** A revenue fact tagged against
`ScenarioForecastMember` is a forecast, not a segment, and the strictness is
what keeps it out.

## The hard part: filers tag two levels of one axis

Apple puts Products/Services on the income statement and iPhone, Mac, iPad,
Wearables, Services in the revenue note — both against `ProductOrServiceAxis`.
Microsoft does the same with Product/Service and eleven product lines. Kept side
by side, the group adds up to twice revenue. Three rules, applied in order and
only when the group *does* over-count (sum > 102% of consolidated revenue):

1. **One filing per period.** Filers rename segments as readily as they re-cut
   them: Microsoft's FY2026 10-K restates the same FY2025 product lines its
   FY2025 10-K reported, with two of them renamed ("Gaming" → "XBOX"). Both
   filings are in hand, so that year would otherwise appear twice under two sets
   of headings. The newest filing restates, so the newest filing wins — unless
   all it mentions of that period is a single member, which is a passing note
   rather than the table. Spans are matched exactly, so a 10-Q's quarter and its
   year-to-date stay distinct.
2. **Pick the table.** `MetaLinks.json`, which EDGAR generates for every inline
   filing, records the presentation roles each member appears under — which is
   what tells the income statement's two-line split from the revenue note's
   eleven-line one. Candidate tables are ranked on adding up to revenue first
   and being the finer split second, so the eleven lines win over the two that
   sum to the same total. If no table resolves the over-count, none is taken:
   reporting the over-count beats picking an arbitrary subset.
3. **Un-nest what is left.** One table can nest inside itself — NVIDIA presents
   Data Center with its Compute and Networking split beneath it. What the tables
   cannot settle, arithmetic does: a member that is *exactly* the sum of two or
   more of its neighbours is a roll-up of them and gives way to the finer split.
   Exact is the point. A 5% tolerance over a dozen nine-digit numbers would find
   a "match" for anything, so only an exact hit counts, and a member merely
   equal to one sibling is a coincidence rather than a parent.

What was replaced comes back in `extra.superseded`, so nothing disappears
silently.

## Where the names come from

The instance names members in QName form (`aapl:IPhoneMember`). "iPhone" is in
the linkbases filed alongside it — `MetaLinks.json` for inline filings, the
`_lab.xml` label linkbase for filings old enough to predate it. Standard members
resolve too, which is the difference between a row reading "United States" and
one reading `country:US`. Labels are fetched from as few filings as will answer:
the newest names almost every member, and older ones are opened only for a
segment retired years ago. Failing everything, the QName is split
(`AmericasSegmentMember` → "Americas"), which is right often enough to beat
printing an identifier.

## A breakdown need not add up

Two ways, both normal, both reported rather than papered over:

- **Over 100%.** Segment revenue is reported *before* sales between segments are
  eliminated. Intel's segments sum to 133% of consolidated revenue because
  Intel Foundry sells to Intel; UnitedHealth's reach 199% because Optum is
  tagged alongside the three businesses inside it *and* sells to
  UnitedHealthcare.
- **Under 100%.** A filer discloses only the split it has. Nike names the United
  States and stops, at 44% of revenue.

`extra.dimensions[].coverage` carries each group's share of consolidated
revenue, and anything outside 95–105% comes back as a warning on the response.
The percentages in the common-size view are shares of consolidated revenue, so
a group that does not add up reads as exactly that.

## Cost and coverage

The first call for a symbol downloads two to four instance documents (1–5 MB
each) plus one `MetaLinks.json`; a filed document never changes, so the parse is
cached on the accession for a week and every later call is local. Typical cold
call is 3–5 seconds, warm is under one. The walk stops as soon as enough periods
are covered — quarterly waits for an annual report as well, because nobody files
fiscal Q4 on its own and it is the full year less the nine months that recovers
it.

History starts where XBRL does (2009–2011, phased in by filer size). Of a
26-name sample of operating companies across every sector, all 26 return a
breakdown and 25 report business segments. ETFs and crypto pairs file nothing
with the SEC and refuse cleanly, saying why.

Banks needed two extra concepts, because a bank's segment table does not report
"revenue": `RevenuesNetOfInterestExpense` is the "total net revenue" line
JPMorgan and its peers tag, and `RevenuesNetOfInterestExpenseFullTaxEquivalentBasis`
is the same line for the several — Bank of America, Wells Fargo — that present
segments on a fully-taxable-equivalent basis and tag nothing else. The FTE
measure ranks below the plain one, so it is only used where a bank tags no
alternative; the adjustment is well under a percent of revenue, and it shows up
in `coverage` where it matters.

## Using it

```bash
mft.equity.fundamental.revenue_segments(symbol="AAPL")                   # all three
mft.equity.fundamental.revenue_segments(symbol="MSFT", dimension="product")
mft.equity.fundamental.revenue_segments(symbol="NVDA", period="quarter", limit=8)
```

```
GET /api/v1/equity/fundamental/revenue_segments?symbol=AAPL&period=annual&limit=6
```

Rows are the same shape the statements use — one row per segment, one column per
period, newest first, each group closing on a `Total disclosed` subtotal with
`Total revenue` under the lot — so the stock page's Segments tab reuses the
statements renderer, and the As-reported / Common-size / Change chips work on it
unchanged.
