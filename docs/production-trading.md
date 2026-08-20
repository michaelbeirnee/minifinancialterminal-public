# Daily production trading

The research stack answers "which predictors survive?". This layer turns that
into a boring, deterministic daily job: signals calculated after the close,
orders executed the next session, research refreshed on its own cadence, risk
model refreshed every ~21 trading days. It is a daily / multi-day US equity
stat-arb pipeline — no intraday infrastructure is assumed or needed.

```text
research engine ──promote──► production_signal_vintages   (frozen registry)
                                        │
                     daily cycle (cutoff today → execute tomorrow)
                                        │
   prices ≤ cutoff ─► live scores ─► sleeve target ─► risk projection
                                        │
                                  RISK GATEWAY          (hard checks, kill switch)
                                        │
                            order ledger ─► broker (ledger | alpaca paper)
                                        │
                     reconciliation: fills → positions → discrepancies
```

## Separation of research and trading

`POST /api/production/vintages/promote` runs the complete multisource research
pass — OOS gates, group-neutral evidence, FDR, execution/capacity tests,
redundancy clusters, sleeve planning — and freezes the surviving blend into the
`production_signal_vintages` registry with its parameters, per-signal evidence
and every gate that produced it. Promoting retires the previous approved
vintage. The daily cycle consumes **only** the latest approved vintage: a new
experiment never touches capital because yesterday's backtest looked good.

A vintage older than `max_vintage_age_days` (45 by default) blocks the cycle —
stale research is treated exactly like stale data.

## The daily cycle

`python -m cli.daily_cycle` (or `POST /api/production/run`) executes one cycle:

1. load the latest approved vintage, else stop;
2. fetch prices up to the information cutoff — the decision bar is the last
   close at or before `as_of`, and nothing after it can enter the target;
3. reconcile first: ingest any fills, rebuild ledger positions from the fill
   ledger, compare with the broker — a mismatch blocks the day;
4. capture today's raw payloads and estimate/option/crowding features into the
   point-in-time store (on by default; `--no-capture` skips);
5. score only the vintage's signals on the truncated panel;
6. build the sleeve target from the vintage's **frozen** sleeve plan;
7. rebuild the factor/covariance risk model only when its refresh is due
   (`factor_risk_refresh_days`, 21 by default) — otherwise the model is
   reconstructed as of its previous date, so the cadence is deterministic;
8. project the target through the full constraint set (dollar/beta neutrality,
   gross, name, borrow/crowding, volatility target, factor caps);
9. read NAV and positions from the broker, diff target shares against held
   shares, apply the minimum-trade threshold, drop unavailable shorts, apply
   one uniform ADV-capacity scale to the whole trade vector;
10. run the gateway; persist the run, its stages and its orders.

Every run row records each stage and each gateway check, pass or fail, so a
blocked day explains itself.

A practical schedule (cron, America/New_York):

```text
35 16 * * 1-5  cd /path/to/repo && .venv/bin/python -m cli.daily_cycle --capture-only
45 9  * * 1-5  cd /path/to/repo && .venv/bin/python -m cli.daily_cycle --reconcile-only
```

`--capture-only` needs no research vintage, so the archive clock starts before
any strategy is approved. Once a vintage exists, swap the afternoon line for
the plain full cycle (which captures first by default, then builds the book).

## Raw observations: log everything, cook later

Derived features answer today's formulas; `raw_observations` keeps the
ingredients. Every capture appends the provider payloads exactly as fetched —
the full 180-field Yahoo profile, every analyst-estimate table, price targets,
recommendations, the selected near/far option chains, recent upgrades and
earnings rows — keyed by both clocks (`as_of_date` effective, `observed_at`
seen). Rows are never updated: a second capture the same day appends. The
feature table stays the daily point-in-time layer research reads; the raw
table is what lets a feature formula change later and be recomputed over
history instead of restarting the archive from zero. `GET
/api/production/observations` summarises what the archive holds.

The capture universe resolves explicit list → latest approved vintage →
`MFT_CAPTURE_UNIVERSE` → a built-in ~120-name liquid US default, because an
uncaptured day is permanently unrecoverable and capture should never wait on
configuration.

## The risk gateway

No order reaches a broker without passing every check: the
`MFT_TRADING_ENABLED` kill switch, the per-run `orders_enabled` flag, gross
and net exposure, beta exposure, predicted portfolio volatility, name
concentration, order count, data freshness, research-vintage freshness, clean
position reconciliation, and a daily-loss circuit breaker against the previous
run's NAV. Any failure leaves the orders recorded as `planned` and submits
nothing. If the database, market feed, broker state or reconciliation
disagree, the system trades nothing — that is the design, not an error path.

## Brokers

* **ledger** (default, zero keys): submitted orders fill at the next session's
  open during reconciliation, with the configured commission/slippage — the
  stage-one simulator, and the permanent internal ledger either way.
* **alpaca**: routes the same limit orders to Alpaca's **paper** API. The
  adapter hard-refuses any host other than `paper-api.alpaca.markets`, same
  line the tick engine draws. Note Alpaca's paper simulator does not model
  market impact or queue position — which is exactly why the internal cost
  model stays on during paper trading, and why every order stores its
  decision price, limit, fill price, fees and unfilled quantity: that ledger
  is the raw material for an empirical cost model later. For a serious
  long/short book, Interactive Brokers' shortable-shares and borrow-fee data
  would slot into the existing `borrow_fee_annual_bps` / `short_available`
  archive fields without changing the simulator.

## Reconciliation and the ledger

Positions are always rebuilt from recorded fills — the database never assumes
an order filled. Broker positions are the source of truth for actual
holdings; `POST /api/production/reconcile` (or `--reconcile-only`) ingests
fills, snapshots both books into `production_position_snapshots`, and reports
discrepancies. The next cycle refuses to trade while a discrepancy stands.

The data flow the tables implement:

```text
raw_observations → point_in_time_features (research_feature_snapshots)
  → signal_values → research vintages
  → target books (production_runs) → orders → fills → positions → P&L
```

## Stage gates before meaningful capital

1. **Record-only** (`orders_enabled=false`, the default): the full pipeline
   runs and records hypothetical orders. Compare live signal distributions,
   predicted volatility and turnover against research.
2. **Paper** (ledger fills, then Alpaca paper): watch realized versus
   predicted costs, fill behaviour, factor neutrality, and whether live IC
   resembles OOS IC across more than one regime.
3. **Small real capital** — only after the first two stages hold up, and only
   by deliberately flipping `MFT_TRADING_ENABLED`; nothing in this codebase
   places real-money orders today, and the Alpaca adapter refuses non-paper
   hosts by construction.
