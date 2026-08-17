# Peer selection and comparison

Status: implemented 2026-08-16. `backend/providers/peers.py`,
`backend/extensions/compare.py`, Compare mode on the stock page, tested in
`tests/test_compare.py`.

A stock page answers "how is this company doing". It cannot answer "is that
good", because that is a question about other companies — and the usual free
answer to *which* other companies is a vendor's industry bucket, which is one
opinion with no evidence behind it and no way to argue with it.

So the peer group is assembled from three sources that disagree, and then the
group is compared on four things at once.

## Who the peers are

| Source | What it is | What it is good for |
|---|---|---|
| **Classification** | Yahoo's industry list (its sector list where the industry is thin) | fast, covers every listed name, ranked by weight in the industry |
| **Registration** | every SEC registrant filing 10-Ks under the same SIC code | the *filer's own* classification, chosen at registration — catches companies the vendor files elsewhere |
| **Filings** | 10-Ks naming this company next to a competition phrase — "we compete with X", "our principal competitors include X" | the only source where somebody has actually said the two compete |

The third is the interesting one, and it is the mirror of the concentration
mining in [supply-chain-relationships.md](supply-chain-relationships.md): six
phrases, each ANDed with the company's name in a separate EDGAR full-text
search, because that search has no OR. A hit is a filer who wrote the sentence,
so the row carries the filing it came from.

## Ranking: agreement, then size

Each source votes with a weight — filings 3, classification 2, registration 1 —
discounted by where the name sat in that source's own order, for the two sources
whose order means something. A name all three return therefore outranks one that
a single source produced, and a registration-only name is halved again: the SIC
list runs to hundreds of companies, most of them tiny.

Then size settles the rest, because **the mentions are asymmetric**. Everybody
names the giant in their industry. A $20m shell listing Pfizer among its
competitors is telling the truth about its ambitions and nothing useful about
Pfizer, and before this discount that shell outranked Eli Lilly. Each order of
magnitude between the two market caps halves what a candidate's evidence is
worth:

```
proximity = 1 / (1 + |log10(subject cap / peer cap)|)
```

Only the two dozen contenders are priced, because a market cap is a request per
company.

The effect, for Pfizer: ARQT, ARVN and AXSM — three small biotechs that name
Pfizer in their risk factors — led the list; afterwards it reads ZTS, ABBV, ABT,
AMGN, MRK, GILD, LLY, JNJ.

## Two things that go wrong, and what is done about them

**Thin industries.** Yahoo files Apple under "Consumer Electronics", where the
next names down are a soundbar company and a headset maker. Where the bucket
holds fewer than twelve names the sector list is appended behind it — a coarser
match, ranked lower, but it puts Microsoft and Nvidia on Apple's list instead of
a $10m shell.

**Stale tickers.** A search hit names the filer as it was written on the filing,
and tickers change: SMART Global Holdings filed as SGH and trades as PENG. The
bracketed ticker is used when it is still listed (it disambiguates a company with
several share classes, which the CIK map cannot), and the CIK map answers when it
is not.

## The comparison

Three commands, meant to be used together:

```bash
mft.equity.compare.peers(symbol="NVDA")                       # who the group is
mft.equity.compare.table(symbol="NVDA,AMD,INTC,AVGO")         # side by side
mft.equity.compare.revenue_mix(symbol="NVDA,AMD,INTC,AVGO")   # what they sell
```

`table` returns one row per metric and one column per company — the shape the
statements use — in three sections:

* **Size & valuation** — market cap, enterprise value, revenue, P/E trailing and
  forward, P/S, EV/EBITDA, P/B, dividend yield
* **Growth & margins** — revenue and earnings growth, gross/operating/net
  margin, return on equity, debt to equity, free cash flow
* **Returns & risk** — total and annualised return, volatility, max drawdown,
  Sharpe, and each company's beta and correlation *to the subject*

The first symbol is the subject: the returns section is measured against it, and
the `median` column is the median of **the others**, so a company can be read
against its group without being averaged into it. Valuation and growth are the
vendor's trailing-twelve-month snapshot; returns, risk and correlation are
computed here from three years of prices.

Two vendor fields are not taken at face value. A dividend yield arrives as a
fraction in some payloads and as percentage points in others, and below 1% the
two are indistinguishable — so it is computed from the annual rate and the price
instead. A market cap is simply missing from some snapshots, and shares times
price is the same number by definition.

`revenue_mix` reads each company's newest filed revenue split out of its own
XBRL (see [revenue-segments.md](revenue-segments.md)) and returns it as shares.
This is the panel that most often ends a comparison: two companies in one
industry bucket can earn their money in completely different places, and the
multiples above stop meaning the same thing when they do.

## In the terminal

Compare mode is the fifth chip on the stock page. The suggested group is the top
four peers; every row in the picker below shows why it is there and links the
filing that said so. Add a ticker by hand, remove one with its ×, and the group
is remembered per symbol — on the account, so it follows you to another browser,
with a local copy so the panel is never empty while that call is out. "Reset to
suggested" forgets the edit.

A peer group is a judgement. The point of the evidence column is that you can
disagree with this one.

## Cost

The first call for a symbol runs six full-text searches, one registrant lookup
and a price for each contender — three to six seconds. The assembled group is
cached for a day and the sources under it for a week, so the second call is
instant. The comparison table is one vendor snapshot per company plus one
batched price history; the revenue mix is the expensive one, since it opens a
filing per company, and it loads on its own so the rest of the page does not
wait for it.
