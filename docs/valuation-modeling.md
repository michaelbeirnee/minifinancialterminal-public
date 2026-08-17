# Modeling: a DCF you drive

Status: implemented 2026-08-16. `backend/valuation/` (engine + seeding),
`backend/routers/modeling.py`, `valuation_models` table, the **Modeling** tab,
tested in `tests/test_modeling.py`.

A discounted cash flow is not hard arithmetic. What is hard is knowing what to
type into twenty boxes, and knowing which of the twenty the answer actually
turns on. So the model opens pre-filled from the company's own filings, shows
the history behind every default, and puts a sensitivity grid under the answer.

## Layers

| Layer | What it does | Why it is separate |
|---|---|---|
| `valuation/dcf.py` | the arithmetic | pure functions over plain numbers — no I/O, so a 25-cell sensitivity grid is 25 cheap calls, and every figure on screen traces to one line |
| `valuation/seed.py` | proposes every assumption from the filings | reads `equity/fundamental/statements`, so the seed improves whenever the statement normalisation does |
| `routers/modeling.py` | seed / value / save / re-run | valuation happens server-side so there is exactly one implementation of the maths |
| Modeling tab | the controls | the whole screen is a function of one `mdAssumptions` object; no control holds state of its own |

## The model

Standard unlevered DCF:

```
revenue_t  = revenue_{t-1} × (1 + growth_t)
EBIT_t     = revenue_t × margin_t
NOPAT_t    = EBIT_t × (1 − tax)
FCF_t      = NOPAT_t + D&A_t − capex_t − Δ working capital_t
EV         = Σ FCF_t/(1+r)^t + terminal/(1+r)^N
equity     = EV − net debt          per share = equity / diluted shares
```

Growth, margin, D&A, capex and working capital each take either one number for
the whole forecast or one per year, which is what lets a model fade growth
without a different shape of request.

Three details that are where DCFs quietly go wrong, and what this does about
each:

* **Working capital is charged on the *change* in revenue, not its level.** A
  business growing 5% ties up a fraction of that 5%. Charging a percentage of
  total revenue every year instead bleeds cash forever and is the commonest way
  a spreadsheet under-values a stable company. There is a test for it.
* **Mid-year discounting applies to the flows, not the terminal value.** Cash
  arrives across the year; the terminal value is a stock sitting at the end of
  year N and discounts over the full N. Also tested.
* **r must clear g.** A perpetuity only converges while the discount rate
  exceeds terminal growth, and it goes numerically silly well before they meet —
  at a 0.5% spread a rounding error moves the answer by a third. Anything
  tighter than 0.5% is refused with an explanation rather than returned as a
  number.
* **A perpetuity is a steady state, and a build-out is not.** Seeded capex is
  the mean of the filed history, which for a company mid-investment-cycle is a
  ramp: Microsoft's FY24–FY26 capex ran 18% → 23% → 35% of revenue, so the mean
  is 25% against depreciation of 8%. Carried into the perpetuity that says
  Microsoft reinvests three times its depreciation forever, and it values the
  company at roughly a third of its price. The arithmetic is right and the
  history is right; the *assumption* is the thing that is wrong. So the model
  reports terminal-year capex above 1.5× depreciation as a warning on the
  result. It does not quietly fade the number — which capex is the real
  run-rate is exactly the judgement the operator is there to make — but a
  default that silently understates by 70% must not pass without saying so.

## Seeding

Every default is read off the filings and reported with the history it came
from, so the operator can see what they are overriding:

| Assumption | Seeded from |
|---|---|
| Revenue growth | trailing revenue CAGR, faded straight-line to the terminal rate; capped at 35% so one explosive year does not become a five-year forecast |
| Operating margin | mean of the last two filed years |
| Tax rate | mean effective rate (tax ÷ pre-tax income), clamped to 0–50% |
| D&A, capex | mean of the last three years as a share of revenue |
| Net debt | total debt less cash and short-term investments, every component read at the *same* balance-sheet date — a line the filer stopped tagging counts as nil and is called out in the notes, because netting this year's debt against last year's cash produces a figure that appears on no filing |
| Cost of equity | CAPM: live 10-year Treasury + beta × 5% equity risk premium |
| Cost of debt | interest expense ÷ total debt, clamped to 1–15% |
| Equity weight | market cap ÷ (market cap + total debt) |

The 5% equity risk premium is the one number with no filing behind it —
Damodaran's implied ERP has sat in a 4–6% band for two decades. It is an input
like any other and can be overridden by editing the cost of equity directly.

## Saving

`valuation_models` stores the assumptions, the resulting valuation, and the
price at the time. The assumptions **are** the model — they are what the
operator authored. The valuation is frozen alongside them anyway so that
`GET /models/{id}/rerun` can show `saved` beside `now`: a model whose answer has
moved without its assumptions changing is telling you something about the
business, and that comparison is the reason to come back to a saved model at
all. Re-running never overwrites the stored answer.

Models are per-user and every query filters on `user_id`; there is a test that
one account cannot read or delete another's.

## What this is not

A DCF is a way of writing down what you believe, not a way of finding out what
something is worth. The terminal value routinely carries 60–80% of enterprise
value, so the result reports that share on its face and warns above 85% — at
which point the explicit forecast is decoration on a perpetuity. The sensitivity
grid exists for the same reason: the honest output of a DCF is a range and a
sense of what moves it, which is why the grid is on the screen next to the
single number rather than behind a tab.
