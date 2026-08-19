# Options greeks and the Black-Scholes pricer

Status: implemented 2026-08-19. `backend/valuation/blackscholes.py` (the math),
two new commands in `backend/extensions/derivatives.py` —
`/derivatives/options/greeks` and `/derivatives/options/pricer` — tested in
`tests/test_blackscholes.py`.

---

The chains, the IV surface and the unusual-activity screen were already here;
what was missing was everything *derived*: no delta on a contract, no theta on
a position, no way to ask "what is this option worth at 25 vol". Yahoo
publishes an implied volatility per contract but no greeks, and no free source
publishes them at all — so, unlike everywhere else in this codebase, the gap
is closed by computing rather than fetching. The model is the one thing here
that cannot rot with a vendor.

## The model, and its stated bias

Black-Scholes-Merton: European exercise, continuous dividend yield, flat rate
and vol per contract, ACT/365. US equity options are American, so the model
**understates** a put (and a call on a high-dividend name) by the
early-exercise premium — worst deep in the money with high rates and long
expiries, negligible for near-dated calls on low-yield names. That bias is
carried rather than half-fixed: a binomial tree would close most of it at the
cost of a slower, less inspectable pricer, and the terminal's job here is
research, not market-making. The `model` field in every response says exactly
what was computed.

Greeks come out in the units a trader expects: delta per $1 of spot, gamma
per $1 per $1, **theta per calendar day**, **vega per vol point**, **rho per
rate point**; all per share, and a listed contract is 100.

## Where the inputs come from

Everything is assembled from sources already in the stack:

| Input | Source |
|---|---|
| spot | Yahoo quote, `last_price` |
| dividend yield | Yahoo quote — published in **percent units** (AAPL `0.35` = 0.35 %), divided down and clamped at 25 % |
| risk-free rate | the Treasury par curve, linearly interpolated at the expiry's horizon; a flat 4 % with a warning if treasury.gov is down |
| time to expiry | ACT/365 to the **16:00 New York close** on expiry day — a 0DTE contract at noon still has time value, where whole-day counting would price it dead |
| volatility | see below |

## `/derivatives/options/greeks`

The chain for one expiry with `iv`, `bs_price`, `delta`, `gamma`, `theta`,
`vega`, `rho` on every row. `iv_source` picks the volatility:

* `provider` (default) — Yahoo's published implied vol, greeks computed at it.
  Fast, and consistent with what the surface command shows. Yahoo's IVs on
  illiquid strikes are frequently junk (a stale quote solved against a stale
  spot); the greeks computed at a junk vol are junk with it.
* `solved` — re-derived from the bid/ask mid (last price where there is no
  quote) with this module's own bisection solver. Slower, consistent with the
  pricer, and honest: a quote below the option's arbitrage floor has **no**
  Black-Scholes volatility, and comes back null rather than pinned to a
  bound. The warning states how many of the chain's contracts solved.

`extra` carries every assembled input — spot, rate, yield, time — so a number
can always be reproduced by hand.

## `/derivatives/options/pricer`

A standalone calculator, the `OVME`-shaped tool: give `sigma` to price, or
`price` to solve the implied vol (exactly one of the two). Spot comes from
`s=` or live from `symbol=`, which also fills the dividend yield; the rate
defaults off the curve. Without `option_type` both sides come back, plus a
`put_call_parity_gap` computed on the unrounded prices that should sit at ~0 —
if it ever does not, the pricer is wrong, which is why it is printed.

```
GET /api/v1/derivatives/options/pricer?s=100&k=105&dte=30&sigma=0.25
GET /api/v1/derivatives/options/pricer?symbol=AAPL&k=320&dte=45&price=12.50&option_type=call
mft.derivatives.options.pricer(k=320, symbol="AAPL", dte=45, sigma=0.25)
```

## Limits worth knowing

* **European model, American contracts** — the bias above, one-directional
  and worst for ITM puts.
* **Yahoo's provider IVs are un-vetted.** `iv_source=solved` is the check:
  where the two disagree materially, trust neither until you know why.
* **Off-hours chains price against a stale spot.** The greeks are only as
  current as the quote and the chain, both of which freeze at the close; a
  0DTE chain read after the close solves to nothing, correctly.
* **Rates are one point on the par curve**, not a bootstrapped zero curve —
  a rounding error at these horizons next to everything above.
