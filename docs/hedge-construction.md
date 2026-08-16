# Design: hedge construction across options and public hedging methods

Status: design v2 (v1 2026-08-13; revised same day after review).
Build-order step 1 is implemented: `backend/portfolio/snapshot.py` (shared
as-of snapshot; /risk and /factors refactored onto it),
`backend/portfolio/hedges.py` (exposure estimators with uncertainty), and
`GET /api/portfolios/{id}/hedge/exposures` in `backend/routers/hedge.py`,
offline-tested in `tests/test_hedge.py`. Step 2 is implemented:
`backend/portfolio/pricing.py` (Black–Scholes with rates/dividends, Black-76,
intrinsic limits, typed `OptionLeg`/`OptionStructure`/`LinearHedge` schemas,
executable-side `entry_cost`, sticky-strike `structure_value` repricer, and
`clean_chain` quote hygiene with majority-anchored monotonicity), tested in
`tests/test_pricing.py`. Step 3 is implemented: `backend/portfolio/shocks.py`
(horizon-matched overlapping joint shocks with mandatory ΔVIX→IV-shift
dimension and beta+residual fallback for short-history holdings;
`book_pnl`/`hedge_unit_pnl` distributions, `cvar_curve` per integer contract
count, seeded `protection_ci`, and the display-only `scenario_grid`), tested
in `tests/test_shocks.py`. Step 4 is implemented:
`backend/portfolio/candidates.py` (chain→candidate construction with tenor
buffer and strike selection, collar sized by shares covered with the <100
share case reported not rounded, integer solver on the CVaR curve, the
corrected cost table — protection_bps with CI / cost_bps decomposed into
bid-ask give-up + decay-to-horizon + carry / ranking on lower-bound
protection — and the "de-risk by selling" verdicts), tested in
`tests/test_candidates.py`. Step 5 is implemented: `POST
/api/portfolios/{id}/hedge/analyze` in `backend/routers/hedge.py` (snapshot →
live ^VIX + chains → shocks → cost table; fetch helpers are module-level and
monkeypatched in tests) plus the HEDGE panel in the Portfolio tab
(`frontend/index.html`, `hg*` controller in `app.js`), browser-verified
against live SPY chains. Step 6 is implemented: the `hedge_records` table
(`HedgeRecord` in `models.py`), CRUD + state-machine + scorecard endpoints in
`backend/routers/hedge.py`, and the HEDGE LOG panel — browser-verified
through the whole `proposed → accepted → executed → closed` walk, with a
test asserting that running the analysis writes nothing. Step 7 is
implemented: `backend/portfolio/overlays.py` + `GET /api/backtest/overlays`
+ the THE LONG RECORD panel — and it surfaced the ^PPUT/^CLL data constraint
recorded below. Step 8 is implemented: `backend/portfolio/narrative.py` +
`POST /api/portfolios/{id}/hedge/narrate` (+ `/narrate/status`) + the
Explain button, keyless-gated like thesis triage. **The build order is
complete.**

The narrative layer is explanation only. Its `validate` pass overwrites any
recommendation disagreeing with the engine's verdict and reports the attempt
in `contradicted_engine`; it drops constructions the engine never priced,
drops a named pick when the verdict is to sell, and flags prose figures
absent from the brief. The endpoint re-runs the analysis server-side rather
than accepting one from the caller, so a narrative can never describe
numbers the engine did not produce.

Two ranking rules were corrected during step 5, after reading real output:
the linear hedge takes the *instrument's* beta (1.0 for the benchmark
itself), not the book's, or residual beta is wrong; and candidates that
reach the risk goal rank above cheaper ones that do not, so a credit collar
missing the target cannot head the table. Companion to
[strategy-test-normalization.md](strategy-test-normalization.md) — the cost
comparison here is the same normalization discipline applied to hedges.

**Core correction from review:** v1 ranked candidates by CVaR reduction while
sizing options by delta and evaluating on a probability-free payoff grid —
i.e. the ranking metric was never actually computed. Delta neutralizes local
linear exposure; CVaR is a distributional tail quantity. The fix is a
**current-contract shock engine** that precedes any ranking: reprice today's
candidate under historical joint shocks and measure CVaR before/after per
integer contract count. Everything below is ordered around that.

## Principle

A hedge is defined against a **measured exposure**, never a vibe, and every
proposal cites which exposure it neutralizes, by how much, with what
uncertainty. Measurement already exists:

- `GET /api/portfolios/{id}/risk` — dollar VaR/CVaR, volatility, drawdown,
  concentration, per-position risk contribution.
- `GET /api/portfolios/{id}/factors` — holdings-weighted betas.
- `backend/extensions/derivatives.py` — live chains with IV (yahoo/cboe),
  expirations, IV surface, futures curves.

## Exposure targets (from one shared snapshot)

| Target | Source | Definition |
|---|---|---|
| **Beta-dollars** | /factors + /risk | value × beta vs SPY (or QQQ/IWM) — linear market exposure |
| **Single-name concentration** | risk_contribution | any position dominating the risk decomposition |
| **Tail loss** | CVaR dollars | conditional loss at chosen horizon/confidence |

Risk and factor numbers must come from **one consistent as-of snapshot**
(today /risk and /factors each fetch their own quotes — a shared snapshot
service is build-order step 1).

## Instrument menu

Ordered roughly by fidelity vs cost:

- **Short index futures / short index ETF** — pure linear beta removal; no
  premium, symmetric. Real costs exist: ETF borrow fee + dividend expense;
  futures basis and roll. Micro futures allow fine sizing.
- **Index puts / put spreads** (SPY, QQQ, IWM; XSP for European-style) —
  asymmetric tail protection; premium bleed. Spreads cap protection, cut
  cost ~40–60%.
- **Collars on concentrated names** — OTM call funds OTM put on the
  dominating position. "Zero-cost" only if **executable bid/ask** actually
  supports it — priced at the touch, not mid.
- **Sector ETF shorts/puts** — when factors show a sector bet in disguise.
- **VIX calls** — convex crash protection, severe contango bleed.
  **Gated on VX futures data feasibility**: priced with Black-76 against
  the VIX future, never Black–Scholes against spot VIX. If a dependable VX
  curve isn't available (CBOE delayed; yfinance is spotty), drop VIX
  options from v1 entirely rather than misprice them.
- **Diversifiers (TLT, gold, …)** — *not hedges*; low-correlation assets,
  labeled as such (2022: the bond "hedge" failed exactly when needed).

## Sizing — different rules per instrument

- **Linear (futures/ETF):** `hedge_notional = beta_dollars / instrument_beta`.
- **Puts and spreads:** solve for the **lowest integer contract count**
  achieving the requested CVaR or stress-loss reduction (via the shock
  engine below). Delta sizing is *not* used for tail hedges.
- **Collars:** size from shares covered; then select floor/cap strikes.
- **VIX options:** Black-76 on the matching VIX future.

Granularity is surfaced, never silently rounded: one standard contract is
100 × spot of notional. **Small-book failure mode is a first-class solver
output** — when the feasible set is {0, 1} and 1 contract over-hedges or
blows the cost budget, the honest answer is "unhedgeable at standard
granularity: XSP, micro futures, or de-risk by selling."

Greeks are not provided by yfinance — Black–Scholes delta/repricing from
chain IV (with rates and dividend yield), pure functions, offline-tested
with canned chains.

## The shock engine (must precede ranking)

Evaluates a contract available **today** against prior shocks — no claim it
could have been traded historically.

1. Freeze one portfolio + quote-chain + exposure snapshot.
2. Build horizon-matched joint shocks: overlapping historical windows of
   (holding returns, index return, Δvol), or bootstrapped factor shocks for
   holdings with short history (map to beta × index + residual bootstrap).
3. **Vol dimension is mandatory, not optional:** pair each window's index
   return with its ΔVIX (or underlying IV proxy), applied as a level shift
   with sticky-strike convention. Frozen IV understates put protection
   (missing vega gain in down states) and silently tilts rankings toward
   linear hedges. Named approximation, disclosed in output.
4. Reprice today's hedge under each shock at t+horizon (reduced expiry —
   theta over the horizon is included; **intrinsic payoff if horizon =
   expiry**, no model needed).
5. CVaR before / after for each integer contract count; select the
   lowest-cost quantity satisfying the requested risk reduction.
6. **Report estimation uncertainty.** ~3y of history at a 21-day horizon is
   ~35 effectively independent windows; 95% CVaR from that is noisy.
   Bootstrap a confidence interval on `protection_bps`; rank on the lower
   bound. Otherwise the exactness trap just moves one layer down.

The deterministic **scenario grid** (index-shock × IV/skew-shock ×
elapsed-time, −30%…+20%) is kept as a *communication/display* tool — it
shows what a construction buys, but it has no probabilities and never ranks.

**Shipped as a curve, not just a grid.** Every sized row in the cost table
carries a `scenario` block: the same −30%…+20% sweep at a finer step, with
the exposure's P&L, the hedge's P&L net of the spread paid, and their sum —
so the UI can draw where protection starts, where a put spread stops paying,
and what the whole thing costs if the move never comes. Two lines, not one:
IV frozen (the pessimistic reading of a long put) and IV where a
least-squares fit of this sample's own ΔVIX-on-return puts it. Where no vol
history exists the second line is *absent* rather than drawn flat — frozen IV
must not be pictured as vol measured and found still. The exposure line is
the whole book for an index hedge (each holding at beta × index) and the one
holding for a hedge written on a name, whose beta has already been spent
getting onto the x axis and must not be applied twice. The tail marker is the
underlying's own historical `level` quantile, labelled as history.

## Cost table (corrected)

Separate observable cost from protection; never merge:

```
cost_bps                 = 10,000 × horizon_cost / portfolio_value
protection_bps           = 10,000 × (CVaR_unhedged − CVaR_hedged_before_cost)
                                   / portfolio_value
cost_per_unit_protection = cost_bps / protection_bps
```

Additional columns per candidate:

- residual beta after hedge
- correlation / tracking error of instrument vs book
- contract count and over/under-hedge amount
- bid–ask execution cost (long legs at ask, short legs at bid)
- liquidity quality (OI, volume, spread)
- upside loss under positive scenarios (separate from cash cost — forgone
  upside is not premium)

**Basis risk is reduced effectiveness, not a cost line.** Joint shocks
already price imperfect correlation into `protection_bps`; adding a basis
"cost" would double-count it.

## Quote hygiene

Filter before pricing: zero-bid, crossed, stale-timestamp, and
strike-monotonicity violations. Full no-arb surface checks (butterfly
convexity etc.) are out of scope for v1 — log as warnings, don't cleanse.
SPY early-exercise caveat stays explicit; XSP is the cleaner European-style
analysis vehicle.

## API

**`GET /api/portfolios/{id}/hedge/exposures`** returns: `as_of`, portfolio
value + currency, benchmark, lookback window, horizon + confidence,
estimator version, exposure uncertainty, source timestamps — all from the
one shared snapshot.

**`POST /api/portfolios/{id}/hedge/analyze`** (POST because the request
carries state): horizon, risk-reduction goal, permitted instruments,
liquidity limits, quote snapshot reference. Returns sized candidates with
the cost table and CIs, each row carrying the `scenario` curve above
(`points`, `tail_shock`, `exposure`, `horizon_date`) for the chart.

**`POST /api/hedge/simulate`** runs the same pipeline over a one-name book
sized in dollars, so its rows carry the same `scenario` block — drawn against
the name's own history rather than an index's.

## Hedge lifecycle log

Deliberately a **different animal from `signal_events`**: that table records
all gated candidates (attention log); this one records **decisions**.

States: `proposed → accepted → executed → rolled → closed/expired`.
A candidate merely displayed in the UI creates **no** record. Persisted per
record: quote snapshot, assumptions/estimator version, target exposure,
expected CVaR reduction (with CI), actual quantity, and later realized
hedge P&L and book P&L — so "was the insurance worth it" is eventually
answered from our own record.

## Reference overlays

^PPUT (5% OTM put protection) and ^CLL (collar) are the honest proxies for
systematically running those hedges. **^BXM and ^PUT are overwrite/put-write
comparators only**: selling premium supplies no loss floor, so neither may
rank beside a protective strategy. The split is carried in the data
(`Overlay.protective`), not left to caller discipline.

**Measured constraint (2026-08-13, contradicting the v1/v2 assumption
above): Yahoo publishes no usable history for ^PPUT or ^CLL.** Probed
directly: `^BXM`, `^PUT`, `^SP500TR`, `^GSPC` and `^VIX` return full
histories; `^PPUT`, `^CLL`, `^CLLZ`, `^BXD`, `^BXN`, `^BXMD` return a single
stale row. So in practice this endpoint usually cannot evaluate *protective*
hedging at all — only the premium-selling contrast. The response therefore
carries `protective_available`, and an empty overlay list is reported as
"could not measure", never as "measured and found wanting". Finding a
protective-index source (CBOE direct, or a total-return reconstruction)
is open work.

The reference is `^SP500TR`, not `^GSPC`: the strategy indexes include
dividends, so a price-return benchmark would flatter every overlay by roughly
the dividend yield per year.

## Build order (revised — shock engine before any ranking)

1. Shared as-of portfolio snapshot + exposure service (refactor /risk and
   /factors onto it).
2. Pricing functions + typed instrument schemas (BS w/ rates+divs, Black-76,
   intrinsic; canned-chain offline tests).
3. Historical-shock distribution engine + deterministic display grid.
4. Integer candidate solver + corrected cost table with CIs.
5. Endpoints (`/hedge/exposures`, `/hedge/analyze`) and UI panel.
6. Hedge lifecycle log.
7. CBOE reference overlays in the backtest router.
8. Narrative layer (triage-style), only after everything above is
   deterministic and tested.

## Honesty notes

- No historical option chains exist in our data. The shock engine evaluates
  *today's* contract against *prior* shocks — stated as such in the UI.
- The IV-shock mapping and sticky-strike convention are named
  approximations, shown in output, never silent defaults.
- CVaR estimates carry sampling error; protection is reported with a CI and
  ranked on the lower bound.
- Inverse ETFs' daily-reset decay is modeled in the cost table, not
  footnoted.
- If every construction is expensive per unit of protection — or the book
  is too small for integer contracts — the honest output is **"de-risk by
  selling, not by hedging."**
