# Supply-chain relationships from SEC filings

Status: implemented 2026-08-16. `backend/providers/supplychain.py`,
`backend/extensions/relationships.py`, Exposure mode on the stock page,
tested in `tests/test_relationships.py`.

Bloomberg's SPLC screen — suppliers left, customers right, revenue exposure on
every node — is built on a proprietary research database that has no free
equivalent. It does, however, have a free *primary source*. Every US filer must
disclose a counterparty that crosses a concentration threshold, and those
disclosures name the company on the other side and put a percentage on it:

> "Direct sales to Apple Inc. accounted for 27.7% of our net sales for the year
> ended December 31, 2023." — Amkor Technology, 10-K

That sentence is the whole feature. It says who, which way round, and how much.
The work is finding those sentences and not mistaking anything else for one.

## How the graph is assembled

Three legs, gathered concurrently; none of them is allowed to fail the others.

1. **Who supplies the subject.** EDGAR full-text search for filings that name
   the subject next to one of ~19 concentration phrases ("of our net revenue",
   "our largest customer", …). One search per phrase, because EDGAR full-text
   search ANDs quoted phrases and has no OR. The union is the candidate pool;
   the number of phrases a filer matches is the first-pass relevance score.
2. **Who buys from the subject.** The same corpus read the other way. A filer
   writing "products purchased from vendors … Apple Inc. 12%" is a *customer*.
   Distributors and resellers land here, which is what fills the right column.
3. **The subject's own annual report**, read for counterparties it names itself.
   This is the only leg that covers a mid-stream company — nobody discloses
   Skyworks as a >10% counterparty, but Skyworks discloses Apple.

Comparables come from the industry classification and are marked as such: they
are not a counterparty and must not read as one.

## Reading a filing

Candidates are only candidates. The precision comes from the parse
(`disclosures_in`), which requires all of:

- **The name is capitalised where it matched.** Keeps "we target 30%" off
  Target Corporation's map.
- **A direction cue.** The sentence has to say money moved — "sales to",
  "revenue from", "purchased from", "of our revenues". A sentence with a name
  and a number and no direction is commentary, not a relationship.
- **A concentration cue near the percentage** ("accounted for", "comprised",
  "of our total revenue"), *unless* the number is within 28 characters of the
  name, which is how a table row reads: `Apple, Inc. | 11 % | 17 % | 19 %`.
- **Proximity**, ≤260 characters between name and number.
- **Not equity boilerplate.** "Purchase" in a 10-K is more often about shares
  than about goods, so stock plans, buybacks and purchase agreements are cut.

Direction is a vote across every disclosure in a filing, not a read of whichever
sentence carried the biggest number: a distributor states its position twice and
one stray label should not flip it. Ties go to `customer`, because `supplier` is
the fallback label applied when no buy-side cue fired at all.

`exposure_pct` is always a share of the books of **whoever wrote the sentence**,
which is not always the subject. `pct_of` names them, and the UI prints it —
"91% of CRUS net sales" and "67% of SWKS revenue" are both on Apple's map and
they mean different things.

## What it cannot see

- **Non-filers.** Coverage is US registrants plus foreign issuers with an ADR
  (20-F). Foxconn assembles most of Apple's hardware and files nothing with the
  SEC, so it cannot appear at any size.
- **Unnamed counterparties.** A filer must disclose that a customer crossed 10%
  of revenue; it does not have to say who. "One customer accounted for 19% of
  revenue" is the norm for mega caps, which is why NVIDIA's map is thin and
  Ford's is not. An unnamed counterparty is a fact about the filing, not a
  company that can be plotted.
- **Anything below the threshold.** A 4% supplier relationship is real and
  invisible here.
- **Currency.** Annual reports, so a relationship can be up to a year stale;
  each node carries its filing date. Jabil named Apple in its FY2024 10-K and
  not in FY2025 — the map follows the filings, and that change is signal.

The honest summary: this reproduces the *shape* of SPLC from public data, with
a citation on every edge, and materially thinner coverage. Every relationship
shown is one a filer put in writing, and one click away from the filing that
says so.

## Cost and caching

A cold company is ~19 full-text searches plus up to 55 filing fetches, about
5–10 seconds. Filings are fetched **uncached** and only the extracted sentences
are stored — caching the documents would put a gigabyte of boilerplate on disk
per few dozen companies looked at. `_PARSER_VERSION` is baked into the cache
keys, so changing any rule above retires every parse made under the old ones.
