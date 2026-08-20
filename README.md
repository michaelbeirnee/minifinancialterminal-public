# Mini Financial Terminal (MFT)

**[michaelbeirnee.github.io/minifinancialterminal-public](https://michaelbeirnee.github.io/minifinancialterminal-public/)** — project site

An **open-source financial research terminal** — an OpenBB-style data platform with
**319 commands** across equities, ETFs, crypto, FX, derivatives, macro, fixed income
and regulatory filings, plus a factor-model engine, a backtester and HTML tearsheets.

Every data source is **free or public-domain**. There is no paid vendor anywhere in
the stack, and the platform is fully functional with **zero API keys configured**.

> Query the same command from four places — REST, Python, the CLI, or the web UI —
> and get the same normalised result.

```python
from backend.core.interface import mft

mft.equity.price.historical(symbol="AAPL", start_date="2024-01-01").to_df()
mft.equity.fundamental.income(symbol="MSFT", period="annual")   # SEC XBRL
mft.fixedincome.government.yield_curve()                        # US Treasury
mft.economy.cpi(transform="pc1")                                # FRED
mft.technical.rsi(symbol="SPY", length=14)
mft.quantitative.performance(symbol="AAPL,MSFT,SPY")
```

---

## Four ways in

| Interface | How |
|---|---|
| **REST API** | `GET /api/v1/equity/price/historical?symbol=AAPL` — one route per command, documented at `/docs` |
| **Python** | `from backend.core.interface import mft` then `mft.equity.price.historical(...)` |
| **CLI** | `python -m cli.terminal` — menu navigation, tab completion, CSV export |
| **Web UI** | `http://localhost:8000` → **RESEARCH WORKBENCH** keeps top-down and bottom-up evidence separate, makes the exposure bridge explicit, and freezes the joined packet into a tracked thesis; **DATA** browses every command; **PORTFOLIO** tracks what you own; **SAVED** holds watchlists, alerts, saved commands and history; any stock page has **FINANCIALS**, **EXPOSURE** and **COMPARE** tabs; **MODELING** builds savable DCFs; **CALENDAR** maps dated events; **FLAGGED** diffs a company's newest filing against its previous one and screens the whole market for accrual changes; **VOLATILITY** reads the fear gauges and sets a name's realised vol against what its options imply; **ASSISTANT** answers questions and runs commands for you; the ticker tape, the Markets board and a **QUOTE MONITOR** workspace box stream live prices; **PLAYGROUND** is a persistent Python kernel in the browser — `mft` preloaded, `live_ticks()` for the real-time tape, sklearn/statsmodels ready — for quick quant research, ML and NLP; **PAPER TRADING** runs one strategy interface against history or the live stream through the same risk gate, paper OMS and kill switch, and a **TICK RECORDER** writes the live tape to Parquet so history accumulates from the day it is switched on |

All four read the same registry, so a command added under `backend/extensions/`
appears in every one of them with no extra wiring.

---

## Command coverage

| Menu | Cmds | What's in it |
|---|---:|---|
| **equity** | 74 | prices, quotes, a live-streamed quote, recorded ticks read back from the local Parquet store (plus DuckDB-built OHLC bars and per-day stats over that tape), performance, profile, search, screener, 10 discovery screens, 5 calendars, 20 fundamental commands (including the three statements as one ordered document and revenue split by segment, geography and product line), 5 estimates, 7 ownership, shorts, dark pool, 3 supply-chain relationship commands mined from filings, and a peer group blended from classification, SIC registration and the filings that name the company as competition — with the side-by-side comparison built on it |
| **technical** | 40 | 35+ indicators: MAs (SMA/EMA/WMA/HMA/ZLMA/DEMA/TEMA), RSI, MACD, stochastic, CCI, ADX, Aroon, Ichimoku, Supertrend, PSAR, Bollinger, Keltner, Donchian, OBV, A/D, CMF, MFI, VWAP, Fisher, TSI, PPO, DeMark, vol cones, Hurst, Clenow momentum |
| **economy** | 47 | CPI, PCE, GDP, unemployment, payrolls, claims, money supply, SLOOS, financial conditions, house prices, trade, debt, country profiles, calendars, surveys — plus `fed/*`, the whole Fed surface read from the Fed's own publications: every hike and cut since 1982 and the cycles they group into, what each cycle did to stocks, bonds and gold, the SEP and the dot plot with the revision against the last one, the statement with its vote, dissents and a sentence diff of what changed in the wording, speeches and congressional testimony, the balance sheet and its runoff pace, the emergency lending facilities, and the days the expected path repriced |
| **quantitative** | 18 | normality battery, unit root, CAPM, rolling stats, Sharpe/Sortino/Calmar/Omega/Ulcer, VaR & CVaR, drawdown |
| **fixedincome** | 15 | Treasury curves, auctions, debt, TIPS, spreads, ICE BofA, Moody's, commercial paper, HQM, SOFR/EFFR/OBFR/IORB, mortgage rates |
| **etf** | 16 | search, profile, holdings, sector & asset-class weights, bond ratings, performance, reverse equity exposure — plus `basket/*` for SPDR funds, which reads the sponsor's own daily file instead of a ten-row summary: every line of the basket with its running weight, concentration (HHI, effective holdings, how few names are half the fund), the GICS industry split under the sector label, return attribution by holding, and weight overlap between two funds |
| **econometrics** | 10 | correlation/covariance, OLS (+ full summary), VIF, Granger causality, cointegration, unit root, autocorrelation diagnostics, panel models (pooled / fixed / between / first-difference / Fama-MacBeth) |
| **regulators** | 10 | SEC CIK maps, registrant search, EDGAR full-text search, SIC codes, press releases, bulk datasets; CFTC COT |
| **charting** | 9 | candlesticks, comparison, drawdown, histogram, correlation heatmap, yield curve, performance bars, vol cones, and a generic "chart any command" |
| **commodity** | 8 | spot prices, futures, complex performance, COT, EIA petroleum/STEO/gas storage |
| **derivatives** | 10 | option chains, expirations, unusual activity, IV surface, put/call snapshots, futures history and term structure — plus computed Black-Scholes greeks on every contract of a chain and a standalone pricer/implied-vol calculator, with spot, dividend yield and the Treasury rate assembled from the free sources already here |
| **index** | 7 | membership for 16 indices, index prices, regional snapshots, sector breakdown, 11 long-run S&P 500 valuation series |
| **crypto** | 6 | prices, ranked market table, global dominance, categories, coin universe |
| **thesis** | 23 | separate stock and sector thesis generators; 17 candidate funnels — insider and congressional clusters, two valuation screens, high growth, quality, free cash flow yield, margin expansion, balance-sheet stress, momentum, dividend growth, estimate revisions, crowded shorts, price dislocations, propagation along disclosed supply-chain links, cointegrated-pair dislocations restricted to those same links, and the market-wide accrual flags from `/flagged` — plus sector rotation, issuer-level detail, and the graded signal log with per-family base rates |
| **currency** | 4 | pairs, history, ECB reference rates, cross-rate snapshots |
| **news** | 5 | company headlines; a newswire tape merged from ~290 feeds in 20 desks (market wires, business, economy, policy and the international press by default; energy, healthcare, tech, real estate, commodities, FX, crypto, opinion and more on request); topic search; feed and desk catalogues |
| **calendar** | 3 | earnings, ex-dividend and payment dates, splits, IPOs, macro releases, FOMC decisions and minutes, and Fed speeches and testimony normalised onto one dated row shape and filterable by type, symbol and size; a ranked, region-filterable economic calendar; and the event-type catalogue, which also names the types no free source can fill |
| **sentiment** | 5 | lexicon-scored news sentiment: market-wide mood, all 11 GICS sectors, per-ticker summaries, story-by-story scores, historical weekly series rebuilt from the Google News archive (feeds the `news_sentiment` backtest strategy — sector ETFs like XLE trade their sector's news) |
| **screener** | 2 | ETF and mutual-fund screeners |
| **overview** | 1 | joined daily market brief with regime, movers, headlines and earnings |
| **research** | 1 | traceable top-down and bottom-up context packet with sector-specific frameworks, an explicit exposure bridge and a source manifest |
| **flagged** | 5 | change detection instead of levels: what moved between a filer's newest filing and its own previous one — risk factors added or dropped, a customer concentration appearing or vanishing, an auditor change, a share count rising against buybacks, deferred revenue diverging from revenue, receivables outrunning sales, a concept tagged for the first time, a one-sided cluster of rating changes — each dated to the day it became public; the three accrual flags computed for the entire market from SEC's cross-company XBRL frames and ranked by percentile; institutional-flow inflections at small caps from SEC's 13F data sets, stated in days of the name's own volume with what the sellers still hold; shared-end-market read-through, where peers disclosing the same geography or product line report the same inflection and one member's consensus has not moved; and the flag catalogue, which states how each flag lies |

`GET /api/v1/_registry` returns the whole surface; `/api/v1/_search?query=…` finds a command.

---

## Data providers — all free

| Provider | Covers | Key |
|---|---|---|
| **Yahoo Finance** (`yfinance`) | prices, fundamentals, options, holders, estimates, screeners, calendars — and its public streamer for live last prices | none |
| **SEC EDGAR** | XBRL fundamentals, segment revenue read from the filings, filings, full-text search, Form 4, 13F, fails-to-deliver, supplier/customer relationships | none |
| **FRED** | US macro, rates, credit spreads (key-free CSV endpoint) | optional |
| **US Treasury / TreasuryDirect / NY Fed** | yield curves, auctions, debt, SOFR/EFFR/OBFR | none |
| **Federal Reserve Board** | the FOMC calendar, statements and minutes, the SEP and dot-plot tables, and the speech/testimony/press feeds | none |
| **BEA** | when each national-accounts report was actually published (PCE, GDP) | none |
| **ECB · World Bank · IMF · OECD · Frankfurter** | euro-area curves, FX, cross-country macro, WEO forecasts | none |
| **FINRA · CFTC** | short-sale volume, ATS/dark-pool volume, Commitments of Traders | none |
| **Senate EFD** | STOCK Act periodic transaction reports — congressional trading (Senate only) | none |
| **State Street (SSGA)** | the full daily holdings file behind every SPDR ETF — the whole basket, not a top-ten summary | none |
| **Stooq · Cboe · Nasdaq · Wikipedia · multpl** | backup prices, delayed option chains, calendars, index membership, CAPE | none |
| **CoinGecko** | crypto prices, market caps, dominance | none |
| **Alpaca Markets** | live trades *and* bid/ask over the free IEX feed — the one optional source behind the live layer | free key, optional |
| **EIA · BLS** | energy reports, labour & price statistics | EIA needs a free key |
| **RSS newswires** | ~290 feeds in 20 desks — Bloomberg, FT, NYT, Economist, CNBC, MarketWatch, Yahoo, Fortune, Forbes, Fox Business, CBS/NBC/ABC business desks, Benzinga, TheStreet, Nasdaq, Seeking Alpha; the Fed, ECB, BoE, BoJ, BoC, RBA, RBNZ, Riksbank, RBI and BIS central-banker speeches; SEC, CFTC, OCC, CFPB, FTC, DOJ, FCA, HM Treasury, USTR, WTO, the Federal Register; BEA, Census, ONS; Nikkei, SCMP, Economic Times, Livemint, Straits Times, Financial Post, SMH, DW, France 24, Sky, City A.M.; sector trades from Rigzone, Mining.com and FreightWaves to STAT, Endpoints, HousingWire, TechCrunch and CoinDesk; and ~30 economics blogs and newsletters — plus Reuters, WSJ, Barron's, Nikkei Asia and Kitco via Google News RSS search. Every feed is checked to parse *and* to be current when added | none |

Only EIA strictly requires a key (free, 30 seconds to get). FRED and BLS work without
one and simply get higher limits or extra endpoints when a key is present — every
key-gated command says so in its error message and names the key-free alternative.

---

## Quick start

Verified on Python 3.14 (numpy 2.5 / pandas 2.3 / FastAPI 0.140); runs on 3.9+.

```bash
pip install -r requirements.txt
./run.sh                       # http://localhost:8000 — register a user, explore
python -m cli.terminal         # or drive it from the terminal
```

```
$ python -m cli.terminal
2026 Jul 25 / $ equity
2026 Jul 25 /equity/ $ price
2026 Jul 25 /equity/price/ $ quote --symbol AAPL,MSFT
2026 Jul 25 /equity/price/ $ export quotes.csv
```

One-shot, without entering the shell:

```bash
python -m cli.terminal "/fixedincome/government/yield_curve"
python -m cli.terminal "/technical/rsi --symbol SPY --length 14"
```

REST:

```bash
TOKEN=...   # from POST /api/auth/login
curl -H "Authorization: Bearer $TOKEN" \
  "localhost:8000/api/v1/equity/fundamental/ratios?symbol=AAPL&provider=sec"
```

### Docker

The whole stack ships as one container image named `5milliondollars`:

```bash
docker compose up -d           # build + run on http://localhost:8000
docker compose down            # stop (state survives in the terminal-data volume)
docker exec -it 5milliondollars python -m cli.terminal   # CLI inside the container
```

The SQLite database and market-data cache live in one shared directory on the
host — `$MFT_DATA_DIR`, default `~/.5milliondollars` — bind-mounted to `/data` in
the container, so rebuilds and upgrades keep your accounts, portfolios, and
history. Optional `MFT_*` keys (Anthropic, FRED, EIA, BLS, Nasdaq,
`MFT_SECRET_KEY`) are read from a `.env` file next to `docker-compose.yml` if one
exists; without it everything except the Assistant tab runs unconfigured. Without
compose:

```bash
docker build -t 5milliondollars .
docker run -d -p 8000:8000 -v "$HOME/.5milliondollars:/data" --name 5milliondollars 5milliondollars
```

### Deploying it online

The image already keeps every mutable thing on `/data`, so any host that runs a
container with a persistent volume works unchanged. `fly.toml` configures one:

```bash
brew install flyctl && fly auth login
fly launch --no-deploy --copy-config          # claims the app name in fly.toml
fly volumes create mft_data --size 10 --region iad
fly secrets set \
  MFT_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  MFT_SEC_USER_AGENT="Mini Financial Terminal you@example.com"
fly deploy
```

Three properties of this app decide the shape of any deployment:

- **It is stateful.** SQLite plus a market-data cache that reaches hundreds of
  megabytes. A host with an ephemeral filesystem loses your accounts on every
  restart, so a real volume is not optional.
- **It must stay up.** The grading sweep in `backend/thesis/scheduler.py` runs in
  process on a 12-hour clock. Scale-to-zero stops that clock, and the calibration
  loop quietly stops filling.
- **It is not small.** Importing the app costs ~240 MB before a single request;
  backtests and factor models go well beyond. 512 MB tiers get OOM-killed.

That rules out static hosts (GitHub Pages, Netlify) and plain serverless
(Vercel, Cloud Run at default settings) for the API. GitHub Pages is the right
home for `docs/` — the landing page — and nothing else here.

Set `MFT_DEBUG=false` on any host reachable from the internet. It arms the
startup check that refuses the shipped `MFT_SECRET_KEY`, which otherwise lets
anyone holding this repo mint a login token for any account.

### One database, four interfaces

The four entry points have to agree on where data lives or they silently split in
two — the container writing to its own volume while `run.sh` writes to
`./terminal.db`, each with its own accounts. `.env` is what keeps them aligned;
copy `.env.example` to start.

| Entry point | Database it opens | Set by |
|---|---|---|
| Docker container | `/data/terminal.db` | `environment:` in `docker-compose.yml`, bind-mounted from `$MFT_DATA_DIR` |
| `./run.sh`, `python -m cli.terminal` | `$MFT_DATA_DIR/terminal.db` | `MFT_DATABASE_URL` in `.env` |

**Run one server at a time.** `run.sh` refuses to start while the container is up.
A long-running server holds a SQLite WAL snapshot and will not see writes made by
another process until it restarts, so two servers on one database will disagree
even though the file itself stays consistent. When the container is running, reach
the CLI *through* it rather than from the host:

```bash
docker exec -it 5milliondollars python -m cli.terminal
```

---

## Research tooling (beyond the data layer)

| Area | What it does |
|------|--------------|
| **Factor models** | Market / momentum / low-volatility factors built from the universe, with per-asset OLS exposures (alpha, betas, t-stats, R²) via `statsmodels`. |
| **Portfolios** | A transaction log with FIFO or average-cost lot matching — cost basis, realised/unrealised P&L, dividends and fees, shorts included. Holdings are *derived*, never edited, so a corrected trade re-states history. The same book then feeds the analytics below. |
| **Portfolio analytics** | Daily **time-weighted returns** (deposits neutralised) plus **money-weighted** XIRR; Sharpe/Sortino/drawdown from the same `risk_metrics` the `/quantitative` menu uses; VaR **in dollars** (historical, parametric, conditional); marginal risk contribution per holding; sector/industry/country tilt; and factor exposure regressed on the whole book rather than one ticker at a time. |
| **Backtesting** | Two engines sharing one cost model: a fast **vectorized** backtester and an **event-driven** engine (explicit cash, positions, commission, slippage, execution latency, per-fill trade log). Both apply a one-bar execution lag to avoid look-ahead bias. Position-sizing overlays (vol targeting, trailing/fixed stop-losses) compose with any strategy. The `stat_arb` strategy blends residual reversal, residual momentum and idiosyncratic-volatility signals into a dollar/beta-neutral long-short target; see [docs/statistical-arbitrage.md](docs/statistical-arbitrage.md). |
| **Backtest analysis** | Parameter **grid sweeps**, **walk-forward** evaluation (rolling train/purge/test folds, per-fold re-fitting, stitched out-of-sample equity), benchmark-relative **attribution** (alpha/beta, tracking error, information ratio, up/down capture), **cost-sensitivity** ladders and block-bootstrap **Monte Carlo** bands on terminal wealth and drawdown. |
| **Signal research** | Independent cross-sectional OOS testing across price, volume, filing-lagged fundamentals, earnings/analyst events, archived estimates/options and trailing peer relationships, followed by sector/industry-neutral evidence, Benjamini-Hochberg false-discovery control and correlation-aware redundancy clustering. Current-only estimate/option data is captured into a point-in-time store instead of backfilled into history; see [docs/signal-research.md](docs/signal-research.md). |
| **Reports** | Tearsheet metrics (CAGR, Sharpe, Sortino, Calmar, max drawdown, win rate) plus a self-contained HTML report with equity and drawdown charts. |
| **Auth** | JWT bearer auth with bcrypt hashing and **revocable sessions** — logout and password changes kill the token server-side. Platform routes require a token unless `MFT_PLATFORM_REQUIRE_AUTH=false`. |
| **Caching** | Graded-TTL cache (memory + disk) in front of every outbound call — 2 min for quotes, a week for reference data. Stats at `/api/system/cache`. |
| **Live prices** | One upstream socket per provider fanned out to any number of Server-Sent-Events readers at `/api/stream/quotes`. Yahoo's public streamer by default (key-free, last price for stocks, ETFs, indices, futures, FX, crypto); Alpaca's free IEX feed when keys are set (licensed trades and bid/ask). The tape, the Markets board and the workspace boxes move on their own; `/equity/price/live` is the same feed as a command. See [docs/live-streaming.md](docs/live-streaming.md). |
| **Playground** | A persistent per-user Python kernel behind the UI: `mft` preloaded with every command, `live_ticks()` collecting the real-time tape, numpy/pandas/scipy/statsmodels/scikit-learn importable, `show()`/`chart()` rendering tables and charts, variables surviving between runs. Runs code as the server user, so it follows `MFT_DEBUG`: on locally, off on a deployment unless `MFT_PLAYGROUND_ENABLED=true`. See [docs/playground.md](docs/playground.md). |
| **Paper trading** | One `Strategy` interface (`on_tick`/`on_bar`/`on_fill`), two feeds: `POST /api/trading/replay` runs it over history — or over bars built from your own recorded ticks — and a live session runs the identical code against the stream, through the same risk gate (notional/gross/loss caps, stale-data refusal, kill switch) and the same paper OMS (explicit order states, next-event fills, spread-aware). Execution is internal by default, or routed to **Alpaca's paper-trading account** when its keys are set — a real venue's fills, reconciled against the local book, hard-limited in code to `paper-api.alpaca.markets`. The parity is tested, not asserted. See [docs/paper-trading.md](docs/paper-trading.md). |
| **Tick recorder** | A stream-hub subscriber that writes every live tick to date-partitioned Parquet under `tick_store/`, flushed on a rows/seconds cadence and swept clean on stop. `MFT_RECORD_SYMBOLS` records from boot; `/equity/price/ticks` reads it back, and DuckDB scans it at scale — `/equity/price/bars_from_ticks` (OHLC bars of any width from your own tape), `/equity/price/tick_stats`, and `tick_db()` in the Playground for free-form SQL. Free real-time data is ephemeral — this is how it becomes owned history. |

---

## Portfolios

The transaction log is the source of truth. Positions are a materialised view of it,
rebuilt in full on every change — so deleting a mistaken fill, or switching a book
from FIFO to average cost, re-states cost basis and realised P&L correctly instead of
leaving a counter wrong.

```
CRUD   /api/portfolios                          books (name, benchmark, cost-basis method)
CRUD   /api/portfolios/{id}/transactions        the blotter
GET    /api/portfolios/{id}/positions           holdings with open tax lots
GET    /api/portfolios/{id}/summary             marked to live quotes: value, day change, P&L
GET    /api/portfolios/{id}/performance         equity curve, TWR + XIRR, metrics, vs benchmark
GET    /api/portfolios/{id}/risk                VaR in dollars, volatility, risk contribution
GET    /api/portfolios/{id}/factors             factor exposure of the whole book
GET    /api/portfolios/{id}/allocation          sector / industry / country / asset-type tilt
```

Sides are `buy`, `sell`, `deposit`, `withdraw`, `dividend`, `fee`, `interest`. Buys and
sells carry shares and a per-share price; the cash sides carry an amount. Commissions
are capitalised into the lot on the way in and netted out of proceeds on the way out,
so realised P&L is already net of costs. Selling more than you hold flips the position
short rather than erroring, and the short is closed by a later buy.

Returns are **time-weighted** day by day, which neutralises deposits and withdrawals —
funding an account is never mistaken for performance. `money_weighted_return_annual`
is the XIRR of the actual cash flows, which is what the account holder earned.

Both are computed from the same engines the rest of the platform uses: `risk_metrics`
in `backend/extensions/quantitative.py` and `factor_regression` in
`backend/factors/models.py`. Pointing them at a reconstructed portfolio return series
rather than a bare symbol is the whole of the new analytics.

---

## Research Workbench

The **RESEARCH WORKBENCH** is the expandable shell between discovery and the
thesis ledger. `GET /api/v1/research/context` assembles one reusable packet with
independent top-down and bottom-up lanes, a transparent mechanical read, and a
manifest of every command and provider used. One unavailable source degrades
coverage without discarding the other evidence.

The joined read does not pretend that a macro theme automatically belongs to a
company. Its exposure bridge requires four links — driver, disclosed exposure,
financial transmission and expectations gap — before the user writes a
falsifiable claim. Creating a tracked thesis freezes the same context command as
point-in-time evidence. The module rail routes into stock ideas, sector ideas,
modeling, catalysts and monitoring, and is the extension point for later
research categories.

---

## The thesis engine

Most of this platform answers questions. The **THESIS** tab is the part that
forms opinions and then holds them to account — an idea is only a thesis here if
it carries the conditions under which it would be abandoned.

It is built bottom-up and deterministic-first. Everything except one triage step
runs without an API key.

```
bulk        SEC quarterly Form 345 archives, ingested market-wide
collapse    folds the filing artefacts that make one decision look like several
families    classifies buyers by relationship, not by checkbox
────────────────────────────────────────────────────────────────────────────
sources     the menu of funnels; insider was the first, not the only
              /thesis/insider_clusters   — the gate; every number computed here
              /thesis/congress_clusters  — the same shape over STOCK Act filings
              /thesis/undervalued_large_caps — low P/E + PEG discrepancy queue
              /thesis/undervalued_growth — growth and valuation disagree
              /thesis/high_growth        — explicit revenue, EPS and market-cap gates
              /thesis/quality_compounders — high ROE and margin, low leverage
              /thesis/cash_generative    — ranked by free cash flow yield
              /thesis/margin_expansion   — profits growing far faster than sales
              /thesis/balance_sheet_stress — distress zone; either side of the trade
              /thesis/momentum_leaders   — near 52w highs; the mirror of dislocations
              /thesis/dividend_growers   — long payout streaks still yielding
              /thesis/estimate_revisions — the one funnel screening a change, not a level
              /thesis/crowded_shorts     — short case or squeeze case; direction-neutral
              /thesis/price_dislocations — large 1m drawdowns that need explaining
              /thesis/link_propagation   — selects on a disclosed relationship, not a company
              /thesis/pair_dislocation   — cointegration only where a filing joins the pair
              /thesis/sector_rotation    — 11 sector ETFs ranked by 3m return vs SPY
```

Sixteen of those are stock funnels and one is a sector funnel; `Source.universe`
is what splits them across the two generator tabs, and `GET /triage/sources?
universe=stocks` is what each tab reads. The frontend renders whatever that
endpoint describes, so registering the eighteenth needs no frontend change.

**Two of them select on an edge rather than a node.** Every other funnel here
ranks companies by a property of the company. `/thesis/link_propagation` starts
from the supply-chain graph mined out of filings — "sales to Company A accounted
for 27% of our net sales" — waits for something material to move at A, and emits
candidates at everyone who disclosed a dependence on it, carrying the disclosed
percentage as the transmission channel and the counterparty's own untouched
estimates as the reason it might not be priced. What moved at A is read three
ways: the next-year consensus (cheap, timely, and only the sell side's view), a
reportable segment shrinking or decelerating two quarters running in A's own
XBRL (slow, and the only channel measuring demand rather than expectations), and
the price. The candidate arrives with its falsifier already attached — *B's
estimates have not moved* is a claim that dies the moment they do — which is why
rows whose estimates already moved are emitted alongside rather than filtered
out: they are the control group the others get graded against. See
[docs/link-propagation.md](docs/link-propagation.md).

**The second turns the same edges into a search space.** Run a cointegration
test over every pair in an index and the 5% that pass by chance outnumber the
ones with a reason to. `/thesis/pair_dislocation` refuses to test a pair unless a
filing joins the two companies first — one discloses the other as a supplier or
customer with a percentage attached, one names the other as competition in its
10-K, or two independent classifications put them in the same segment — and only
then fits the log-price relationship, tests it with Engle-Granger, and reads the
most recent quarter against it *out of sample*: the hedge ratio, the sigma and
the cointegration test all come from history the recent window never touched.
Pairs at least `z_threshold` sigmas from that fit are emitted as `dislocated`
(the whole window still cointegrates: stretched) or `broken` (it no longer does:
something may have changed), the leg that moved less is the candidate, the
sentence that admitted the pair is on the card, and the result reports how many
pairs were tested so the reader can see how many chances the search had to fool
them. Restricting the search to economically justified pairs is what keeps this
from being data mining. See [docs/pair-dislocation.md](docs/pair-dislocation.md).

```

triage      one structured model call: rank, and add world knowledge as hypotheses
deep dive   a tool loop that verifies a candidate and proposes falsifiers
spine       the thesis itself: frozen evidence, executable checks, derived status
memory      record → grade → base rates → back into triage
```

**An idea source is a registration, not a branch.** `backend/thesis/sources.py`
holds what varies between funnels — the command that scans, the tunables it
accepts and their clamps, the lines it renders on a card, and `artifact_rule`:
the way *this* funnel characteristically produces false positives. Insiders
trade on calendar, not conviction; a congressional "cluster" is often unrelated
trades whose 45-day deadlines fell in the same week; cheap screens can be peak
earnings in the denominator; a high return on equity is often just a denominator
shrunk by buybacks; profits outrunning sales is as easily a one-off cost cut as
operating leverage; a momentum screen selects on the outcome it is being asked
to predict; a crowded short can become either a short or a squeeze; a drawdown
does not reveal its cause; and a disclosed link is exactly as old as the filing
it came out of, sized against a whole company rather than against the hub
segment that actually moved. Each source states its own failure mode and
it lands in the triage prompt as the rule the model must argue past, so nothing
below the funnel needs to know which scanner ran.

One thing every screen-backed funnel has to work around: **Yahoo's screener does
not return the fields it was filtered on.** A screen gated on EBITDA growth
answers with an ordinary quote payload that has never carried it, so reading the
gate back off the row yields `None` — silently, forever. Each funnel therefore
gates with the screen and then reads its own numbers back from the company
profile before grading anything.

Everything downstream is shared: the card frame (symbol, price context, measured
base rate), the anti-slop pass, the deep dive, the spine, the graded log. A new
source declares itself, records its emissions under its own namespace, and
inherits all of it. `GET /api/theses/triage/sources` is the menu; the params it
advertises are exactly the query keys `POST /triage` will honour for that source,
and anything belonging to another source is ignored rather than passed on.

**Families, not checkboxes.** IAC files Form 4s at MGM ticking only
`isTenPercentOwner`, which reads as a passive whale — but IAC's chief executive
sits on MGM's board and files his own Form 4s there as a Director. So IAC buys
with board representation and full information rights, which is a different
signal from an index fund crossing a threshold. Nothing on IAC's own filing says
so; the fact lives in the relationship graph across filings, and the bulk archive
already contains it. `backend/thesis/families.py` is that join.

**A thesis is falsifiable or it is not a thesis.** The lifecycle is narrow: it is
created open, accumulates point-in-time evidence snapshots and executable
falsifiers, and is graded by `POST /{id}/evaluate`, which re-runs every check
through the same registry every other interface uses. A check names a command, a
field and a breaking condition — and the comparator describes *failure*, so
`close lt 5.0` means the thesis is broken, not confirmed. One breached falsifier
breaks it. Past its review date with nothing breached, it is `supported`: it
survived every way it promised it could fail. The only hand-set terminal state
is `closed`; the rest is earned from data.

Evidence is immutable once written, and evidence proposed by a model is re-run
and frozen **server-side** — the model's citations are instructions, never data.

**A falsifier is run before it is trusted.** Registering a check used to
validate only that the command exists and the comparator is legal, which let
through two checks that are worse than none: one naming a field the command
does not return, which sits at `holding` until the first sweep quietly turns it
to `error`; and one that is *already* true, which breaks its thesis on the first
evaluation and files it on the scoreboard as a failure that never had a chance.
Neither is detectable without running the thing, so it is run — at the door, on
both the manual and the deep-drafted path. An unreadable check is refused, an
already-breached one needs `allow_breached=true`, and an accepted one starts
life with a reading rather than a null.

**Draft is a state, not a title prefix.** A deep-dive draft is born with
`reviewed_at` null and `GET /api/theses?reviewed=false` is the review queue.
Grading is unaffected either way: review decides whether a thesis is *yours*,
never whether it counts.

```
CRUD   /api/theses                              the spine (works with no API key)
GET    /api/theses?reviewed=false               the review queue: drafts nobody has read
POST   /api/theses/{id}/evidence                run a command now, freeze its rows
POST   /api/theses/{id}/checks                  add a falsifier — run before it is stored
POST   /api/theses/{id}/evaluate                re-run every check, derive status
GET    /api/theses/triage/sources                the funnel menu and each one's params
POST   /api/theses/triage?source=…               rank candidates  ·  needs a key
POST   /api/theses/deepdive?create_draft=true   verify one, draft it into the spine
POST   /api/theses/signals/grade                stamp elapsed outcomes by hand
GET    /api/v1/thesis/signal_report             measured base rates per family, and lift
```

### The calibration loop

The engine records everything it emits, grades it once the horizons have actually
elapsed, and feeds the result back to the stage that ranks. That loop is the
point: it is what stops the gate weights being guesses.

- **Record.** Every gated cluster lands in `signal_events`, keyed on
  (family, symbol, filing date) so re-scanning refreshes rather than duplicates.
  Theses go in too, anchored at their creation date — the engine's own output is
  measured on the same ruler as its inputs, so a deep-dive draft cannot quietly
  escape the scoreboard.
- **Grade.** Outcomes are *measured, never predicted*: entry is the first close
  after the market could know, and each horizon (1m/3m/6m/12m) is excess return
  against the event's benchmark, written only once enough calendar time has
  passed. A background sweep runs every `MFT_GRADING_INTERVAL_HOURS` (12 by
  default, `0` to switch it off and grade by hand).
- **Learn.** `signal_report` turns the graded log into per-family hit rates and
  mean excess. Triage cards carry the line for their own family, so the model
  argues against a measured prior rather than a hardcoded warning — and a family
  with fewer than ten graded events gets no line at all, because a hit rate over
  nine events is noise wearing a percentage sign.
- **Audit the expensive half.** Rows under `thesis:*` also carry `lift_*`: the
  theses the engine built, minus the pooled record of the scanners they were
  built from. That is the question the model stages have to answer — a deep-dive
  draft is only worth its tool calls if it beats what the funnel emitted
  unaided. Both sides need ten graded events before a lift is stated, because
  otherwise it is two noisy numbers subtracted.

Promotion means "worth a human's investigation time". These are attention
signals, not alpha signals, and a world-knowledge leg is an unverified hypothesis
until something checks it.

**What the model sees beyond the funnel.** A triage card is a shared frame
around whatever the source computed, plus two cross-source lines that change
what a cluster *means*: the company's own self-disclosed customer concentration
(insider buying at a supplier whose single customer is 91% of its revenue is not
the same claim as the same cluster at a diversified name), and what members of
the Senate disclosed on the symbol under the STOCK Act. The second is a
different population under a different statute, never corroboration of the
first. Both are off with `relationships=false` / `politicians=false`, and both
make the first scan on a cold cache slow and every later one fast.

---

## Flagged — change detection

Every screen above measures a level. Levels are the commodity part of market
data: the number is on the tape, every vendor sells it, and by the time a screen
can rank on it the ranking is common knowledge. What is not commodity is the
**delta between two filings by the same filer**, because computing one means
holding both documents open and knowing which parts are comparable. That is
what `/flagged/*` does — twelve flag types, ten of them a diff of a company's
newest filing against its own previous one and two that read across filers:

```
GET /api/v1/flagged/scan?symbol=NVDA&kinds=all              every flag for one company
GET /api/v1/flagged/market?screen=receivables&year=2025     one accrual flag, every SEC filer
GET /api/v1/flagged/flows?direction=distribution            13F flow vs the tape, every small cap
GET /api/v1/flagged/read_through?symbol=AMAT                 peers' disclosures as a laggard's evidence
GET /api/v1/flagged/catalogue                                what each flag compares, and how it lies
```

| Flag | Read from |
|---|---|
| Risk factor added / removed — Item 1A paragraphs with no counterpart in the other year's report | two 10-K / 20-F documents |
| Concentration appeared / vanished — a customer, supplier or receivable concentration stated in one annual report and not the other | two annual reports |
| Auditor change — an 8-K Item 4.01, or the PCAOB firm id on the cover page changing | filing index + inline XBRL |
| Share count against buybacks — cash out for repurchases while the diluted count still rose | XBRL facts |
| Deferred revenue diverging from recognised revenue | XBRL facts |
| Receivables outrunning sales — DSO rising | XBRL facts |
| Accounting concept tagged for the first time | XBRL facts |
| Sell-side ratings moved one way — a cluster of dated actions, or the consensus mix drifting | Yahoo (the one vendor-fed flag) |
| Institutional flow against the tape — a 13F position change at a small cap, in days of the name's own volume, with the sellers' remaining overhang | SEC 13F data sets + the tape |
| Shared end-market read-through — peers disclosing the same geography or product line report the same inflection; one exposed member's consensus has not moved | peers' filing XBRL + consensus |

Two properties make the set worth having together. **Every flag is dated** —
the filing date is the first day anyone outside the company could have known —
so a flag drops straight into the graded signal log with an honest `known_on`
and earns or fails to earn a base rate exactly like every other idea source;
nothing here asserts a flag predicts anything, the log will say. And **the
numeric half is computable for the whole market without a vendor**: SEC's XBRL
frames endpoint answers "every filer's value for this concept in this period" in
one request, so `/flagged/market` covers a couple of thousand filers in four to
six requests and each row carries its percentile in the distribution the screen
just computed. The market screen is registered as an idea source
(`flagged_market`), so it triages through the same one-call model stage as
every other funnel.

Most of the code is not the diffs; it is what makes a diff honest. Item 1A is
found by heading-shaped markers so a late "see Item 1A" cannot claim the rest of
the filing; paragraphs match on bigram *or* content-word overlap with thresholds
set against a filer that rewrote its whole risk section, and a balanced
addition/removal count is labelled a rewrite and scored down; concentration
sentences are reduced to *role | basis | counterparty-or-quantifier* keys, with
the disclosure threshold ("10% or more") skipped and the comparative year's
sentence dropped; the auditor is read from the `dei:AuditorFirmId` inline-XBRL
tag, because a firm that renames itself keeps its PCAOB number; the market join
insists that a balance and a flow end in the same period, because SEC's calendar
frames pair a June year-end's December balance with its June revenue; and every
market list ranks on the *capped* measure, then by size, so the top is not the
filer with the most pathological denominator. `docs/flagged.md` has the whole
argument and what the tops of the lists actually turned out to be.

Two of the flags read across filers rather than within one. **Institutional
flow against the tape** aggregates every 13F filer's position per issuer from
SEC's structured data sets — filers present in both quarters only, so a manager
crossing the reporting threshold is not a trade — and states the change in *days
of the name's own average volume*, because at a small cap the entry or exit is
the liquidity event and what the sellers still hold (`overhang_days`) is the
forecastable part; index managers' share, probable PIPEs, single implausible
filers and dual listings are labelled and scored down. **Shared end-market
read-through** clusters a company with the peers whose revenue notes disclose
the same geography or product line, finds the lines where several members
report the same inflection in the same fiscal cohort, and names the exposed
member whose consensus has not moved — the peers' disclosures are the evidence
for its claim, and its print is the catalyst.

The catalogue is the point. Every flag type states how it characteristically
produces a false positive — a season's boilerplate arriving across a whole
industry, a stock-financed acquisition swamping a year of buybacks, an ASC 606
tag migration reading as a deferred-revenue collapse — and the Flagged view
prints that note under every row. A reader should start from the objection, not
the headline.

---

## Database

SQLite by default; point `MFT_DATABASE_URL` at Postgres and the same schema is
created unchanged. `init_db()` creates missing tables *and* adds columns that
models have grown, so an older `terminal.db` keeps working after an upgrade.

| Table | Holds |
|---|---|
| `users` | Login credentials (bcrypt), profile, active flag, login count and last login |
| `user_sessions` | One row per issued token (`jti`, IP, user agent, revoked-at) — what makes logout real |
| `user_settings` | Per-user key/value preferences, stored as JSON |
| `saved_commands` | Named, re-runnable commands with their parameters, favourite flag and usage counters |
| `command_runs` | Every `/api/v1` call: path, parameters, provider, rows, duration, status, error |
| `watchlists` / `watchlist_items` | Named symbol lists with per-symbol notes and ordering |
| `alerts` | Saved price conditions, with last-checked / last-triggered state |
| `backtest_runs` | Persisted backtests and their metrics |
| `research_feature_snapshots` | Dated analyst-estimate and option-chain features captured point in time for future OOS research |
| `portfolios` | A book: base currency, benchmark, cost-basis method, derived cash balance |
| `positions` | Holdings derived from the log — quantity, cost basis, open tax lots, realised P&L |
| `transactions` | The blotter: buys, sells, deposits, withdrawals, dividends, fees, interest |
| `theses` | A falsifiable claim: symbols, direction, review date, prior, derived status |
| `thesis_evidence` | Point-in-time command output frozen against a claim — immutable by intent |
| `thesis_checks` | Executable falsifiers: command, field, comparator, threshold, breach state |
| `signal_events` | Every emitted signal, plus realised excess returns once each horizon elapses |
| `signal_runs` | One row per scan: parameters, events seen, events new, duration |
| `triage_records` / `deepdive_records` | Every model verdict, declines included |

Every query is scoped by `user_id`, so one account can never read or mutate
another's rows. Command history is capped per user
(`MFT_MAX_HISTORY_ROWS_PER_USER`, default 500) and pruned as new runs land.

```
GET/PUT/DELETE  /api/user/settings                 preferences
GET/POST/PATCH  /api/user/saved  ·  POST /api/user/saved/{id}/run
GET/DELETE      /api/user/history          GET /api/user/stats
CRUD            /api/user/watchlists  ·  GET /api/user/watchlists/{id}/quotes
CRUD            /api/user/alerts     ·  POST /api/user/alerts/evaluate
CRUD            /api/portfolios      ·  CRUD /api/portfolios/{id}/transactions
GET/DELETE      /api/auth/sessions   ·  POST /api/auth/logout · /api/auth/password
GET             /api/system/database       tables, columns, row counts
```

Alerts are evaluated **on demand**, so a "triggered" result reflects the moment
you asked. The one thing that does run on a clock is signal grading, and only
because a base rate nobody computes is a base rate nobody has.

---

## The assistant

The **ASSISTANT** tab is a chat window that knows this codebase. It explains
concepts — what a Sharpe ratio measures, why an inverted curve gets attention,
how to read a factor regression — and it can reach into the platform to answer
with your data instead of from memory.

```
you  › Is the yield curve inverted, and why does that matter?
      ⟨ /fixedincome/government/yield_curve ⟩  ⟨ /fixedincome/spreads ⟩
mft  › Slightly, at the short end… ‹answer built from the rows it just pulled›
```

It gets its knowledge of the platform the same way every other interface does —
from the registry:

| It knows | Because |
|---|---|
| every command, grouped by menu, with its summary | the system prompt is **generated from `REGISTRY`** at import time |
| what each menu is for | it reuses `MENU_GUIDES` from `backend/core/docs.py` |
| exact parameters and a worked example | the `describe_command` tool reads `CommandSpec` and `docs.example_for()` |
| live numbers | the `run_command` tool calls `registry.execute()` — the same entry point the REST layer uses |
| your holdings and watchlists | the `get_user_context` tool, filtered on `user_id` like every other user query |

So a command added under `backend/extensions/` becomes something the assistant
can find and run with no extra wiring — the same property that gives it a REST
route and a CLI entry.

**What it can't do:** all four tools are read-only, so it can read your
portfolio but never trade, edit or delete. It answers questions about markets
and about this software; it does not give investment advice.

Two implementation notes worth knowing:

* The whole command index is sent behind a **prompt-cache breakpoint**, so it is
  a cache read rather than ~6k fresh input tokens on every message.
* Replies stream over SSE (`POST /api/assistant/chat`), including a chip per
  command as it runs — an answer that quietly ran five commands should show you
  which five. Transcripts live in the browser tab and are not persisted.

This is the **only paid dependency in the stack**. Leave `MFT_ANTHROPIC_API_KEY`
unset and the tab explains that it is switched off; every data command, the
backtester, the factor engine and the portfolio ledger are unaffected.

---

## Architecture

A command is a decorated function. Registering it once produces the REST route, the
Python attribute, the CLI menu entry and the OpenAPI docs — which is how the platform
carries a few hundred commands without a few hundred hand-written routes.

```python
@command("/equity/price/quote", providers=("yahoo",), summary="Snapshot quote")
def price_quote(symbol: str, provider: Optional[str] = None) -> Result:
    src = resolve_provider(provider, ("yahoo",))
    return Result([yahoo.quote(s) for s in norm_symbols(symbol)], provider=src)
```

```
backend/
  core/          registry.py   command decorator, lookup, execution
                 api.py        walks the registry -> FastAPI routes
                 interface.py  walks the registry -> `mft.equity.price.…`
                 http.py       cached HTTP, retries, per-host throttles & user agents
                 models.py     Result / MFTObject (results, provider, warnings, extra)
                 caching.py    graded TTLs        utils.py  JSON coercion, date/symbol helpers
  providers/     yahoo, sec, fred, treasury, intl, finra, markets, coingecko,
                 govstats, newsfeeds, spdr — one module per data source, no command logic
  extensions/    equity, equity_fundamental, etf, basket, crypto, currency, derivatives,
                 index, news, sentiment, economy, fixedincome, commodity, regulators,
                 technical (+ indicators.py), quantitative, econometrics, charting
  assistant/     prompt.py     system prompt generated from the registry
                 tools.py      the four read-only tools the model can call
                 service.py    the streaming chat loop
  models.py database.py auth.py          SQL schema, engine wiring, JWT + sessions
  routers/       auth, user (saved actions), portfolio, data, factors, backtest,
                 reports, system, assistant, stream (live quotes over SSE)
  stream/        hub.py        one upstream socket per provider, fanned out to readers
                 sources.py    Yahoo's streamer (key-free) and Alpaca's IEX feed (keyed)
  backtest/ factors/ portfolio/ reports/ the research + accounting stack
cli/terminal.py  interactive shell (stdlib only)
frontend/        index.html, styles.css, app.js — no build step
tests/           auth, user_data, data, backtest, factors, platform, indicators,
                 sentiment, portfolio, assistant
```

Results are always normalised into an `MFTObject`:

```python
obj = mft.equity.price.historical(symbol="AAPL")
obj.results     # list of JSON-safe row dicts
obj.provider    # which source actually served it
obj.warnings    # per-symbol failures that did not kill the request
obj.to_df()     # date-indexed DataFrame
```

### Charts without a chart library

`/charting/*` emits Plotly-compatible JSON (`{"data": [...], "layout": {...}}`) built by
hand, so nothing depends on a plotting package being installed. Render it with
plotly.js, `plotly.io.from_json`, or the web UI's Chart.js bridge.

---

## Configuration

Environment variables, prefixed `MFT_` (see `backend/config.py`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `MFT_SECRET_KEY` | dev key | **Override in production.** JWT signing key; the app refuses to boot on the default once `MFT_DEBUG=false`. |
| `MFT_DEBUG` | `true` | `false` marks an internet-reachable deployment and arms the secret-key check above. |
| `MFT_ALLOW_REGISTRATION` | `true` | `false` closes `POST /api/auth/register`. Set it on any public host once your own account exists. |
| `MFT_CORS_ORIGINS` | unset | Comma-separated browser origins allowed to call the API cross-origin. Empty means same-origin only — needed only if the frontend is hosted apart from the API. |
| `MFT_DATABASE_URL` | `sqlite:///terminal.db` | SQLAlchemy URL. |
| `MFT_CACHE_TTL_SECONDS` | `3600` | Default cache lifetime. |
| `MFT_PLATFORM_REQUIRE_AUTH` | `true` | Require a bearer token on `/api/v1/*`. |
| `MFT_MAX_HISTORY_ROWS_PER_USER` | `500` | Command-history rows kept per account. |
| `MFT_SEC_USER_AGENT` | placeholder | SEC asks automated clients to identify themselves — put a real name/e-mail here before running at volume. |
| `MFT_FRED_API_KEY` | unset | Free. Unlocks FRED series search and release dates. |
| `MFT_EIA_API_KEY` | unset | Free. Required for the EIA energy reports. |
| `MFT_BLS_API_KEY` | unset | Free. Raises BLS daily limits. |
| `MFT_ALPACA_API_KEY` / `MFT_ALPACA_API_SECRET` | unset | Free, no funded account. Adds Alpaca as a live-price source with licensed bid/ask. |
| `MFT_ALPACA_FEED` | `iex` | `iex` (free), `sip` (paid), `delayed_sip`, `test`. |
| `MFT_STREAM_DEFAULT_PROVIDER` | `yahoo` | Source for a stream request that names none; `alpaca` is honoured only once its keys are set. |
| `MFT_ANTHROPIC_API_KEY` | unset | **Paid.** Switches on the Assistant tab and the thesis triage / deep-dive steps. Nothing else in the platform uses it. |
| `MFT_ASSISTANT_MODEL` | `claude-opus-5` | Model backing the assistant. |
| `MFT_ASSISTANT_EFFORT` | `medium` | `low`…`max` — how hard it thinks per reply. |
| `MFT_ASSISTANT_MAX_TOOL_ROUNDS` | `6` | Command calls allowed before it must answer. |
| `MFT_GRADING_INTERVAL_HOURS` | `12` | Hours between signal-grading sweeps. `0` disables the clock. |
| `MFT_PLAYGROUND_ENABLED` | unset | The Python playground. Unset follows `MFT_DEBUG` (on locally, off deployed). It executes code as the server user — enable on a public host only deliberately. |
| `MFT_PLAYGROUND_TIMEOUT_SECONDS` | `120` | Wall-clock ceiling per playground run; exceeding it kills the kernel. |
| `MFT_RECORD_SYMBOLS` | unset | Comma-separated tickers the tick recorder starts writing at boot. Empty = recorder idles until started from the API/UI. |
| `MFT_TICK_STORE_DIR` | `tick_store/` | Where recorded ticks live (date-partitioned Parquet). Deliberately not under the clearable cache. |
| `MFT_GRADING_BATCH_SIZE` | `500` | Events examined per sweep; grading is incremental. |

---

## Tests

```bash
python3 -m pytest -q                        # everything
python3 -m pytest tests/test_indicators.py  # indicator maths, no network
python3 -m pytest -k "not live"             # skip live-provider checks
```

`tests/test_indicators.py` and `tests/test_assistant.py` are deterministic and
offline — the assistant tests drive the chat loop against a stubbed client, so
they need neither network nor an API key. The rest pull live data, so
they need network access.

---

## Known limits

What is blocked at the source, what will rot and what is still an open call is
tracked in [docs/future-fixes.md](docs/future-fixes.md), with what would close
each one.

* Yahoo's endpoints are unofficial and rate-limit under load; commands report which
  provider served them, and fall back where a second free source exists (Stooq for
  prices, SEC for fundamentals).
* `/etf/equity_exposure` scans a fixed ETF universe because no free source publishes a
  reverse holdings index — pass `universe=` to widen it.
* `/etf/basket/*` reads each fund sponsor's own daily holdings file, and only State
  Street's is wired up, so full-basket coverage is SPDR funds — which is what the
  sectors view runs on. Every other ETF still has Yahoo's ten-row `/etf/holdings`.
* `/etf/basket/contribution` weights each holding by the share it held when the window
  opened, backed out of today's published weight. That assumes the position's share
  count did not change in between, so index additions and the quarterly rebalance leak
  into the residual — which the command reports rather than absorbs.
* Panel econometrics are estimated with OLS on the appropriate transform; the random
  effects estimator is not implemented (it needs `linearmodels`).
* EIA commands need a free key; the crude/gas/nat-gas *prices* they wrap are available
  key-free through `/commodity/price/spot` (FRED).
* The thesis engine's base rates start empty and stay silent until a family has ten
  graded events, which takes real calendar time — the shortest horizon needs ~35 days
  to elapse before it can be stamped at all. A fresh install has a working loop with
  nothing in it yet, and that is the honest state rather than a bug.
* `families.py` can only see people who have *filed*. A newly appointed director who
  has not yet filed a Form 4 at the issuer is invisible to the relationship join until
  they do; passing the issuer's fresh rows closes most of that gap.
* The SEC bulk archive lags roughly a quarter. `/thesis/insider_activity` reads one
  symbol's fresh filings straight from EDGAR when that lag matters.
* A peer group is a judgement, and this one is assembled rather than looked up: an
  industry classification, the SIC code a company chose when it registered, and the
  filings that name it as competition. Each has a known weakness — vendor buckets are
  thin for mega-caps (Apple's holds eight names, so the sector list is appended behind
  it), SIC codes are self-selected and rarely updated, and EDGAR full-text search matches
  a phrase and a name anywhere in the same document rather than in the same sentence, so
  a filer's SIC code is used to throw out the unrelated ones. Every row says which
  sources found it and links the filing that named it, and the group is editable and
  remembered per symbol. See [docs/peer-comparison.md](docs/peer-comparison.md).
* No free source publishes market-implied rate-hike probabilities. CME's FedWatch reads
  them out of fed funds futures, which are licensed data, so `/economy/fed/*` reports no
  implied percentages at all rather than inventing them from something else. What it does
  give is the market's own rate: `stance` puts the 2-year Treasury next to the target
  midpoint, which is the same directional read — a 2-year well below the midpoint is the
  market pricing cuts. The FOMC calendar page carries about six years of meetings, so
  `/economy/fed/meetings` starts there while the decisions in `rate_changes` run back to
  1982. Press-conference *tone* is out for a different reason — the transcripts are PDFs
  and this project has no PDF dependency — so the language flags and the sentence diff in
  `/economy/fed/statement` run on the statement, which is the document the committee
  actually voted on. See [docs/fed-policy.md](docs/fed-policy.md).
* CPI and the jobs report have no key-free release *schedule*: the BLS returns HTTP 403 to
  automated readers on both its calendar pages and its own RSS. BEA's feed covers PCE and
  GDP, and a free `MFT_FRED_API_KEY` fills in the official US release calendar. So
  `/economy/fed/data_reaction` lists every day the 2-year Treasury repriced and names the
  events it can date, marking the rest `none` — which means "nothing this platform can
  date", not "nothing happened". The link between a move and an event on the same day is
  left to the reader rather than asserted.
* The calendar fills **nine of the sixteen** event types a terminal usually lists.
  Earnings calls, sales results, conference appearances, shareholder meetings, corporate
  access, analyst marketing and deal roadshows come from IR feeds and broker calendars
  with no free public equivalent. They are still returned by `/calendar/event_types`,
  marked unavailable with the reason, and drawn greyed out — a filter that silently
  omitted them would read as "nothing scheduled" rather than "no source". Dividends
  exist only as a per-day feed on a host that is politely rate-limited, so a cold month
  of them takes about a minute and is cached after; the UI says so before the wait and
  defaults to the fast source. See [docs/calendar.md](docs/calendar.md).
* Revenue segments are read out of the filings themselves, because the XBRL company-facts
  API publishes every fact with its dimensions stripped off. Coverage therefore stops
  where a filer's tagging does: a company that only ever cross-tabs its segments against
  geography (Exxon does) reports none here, since adding a cross-tab cell to the
  single-axis rows would count the same revenue twice. A breakdown also need not add up
  — segment revenue is reported before sales between segments are eliminated — so each
  group carries its coverage and anything outside 95–105% comes back as a warning. See
  [docs/revenue-segments.md](docs/revenue-segments.md).
* Congressional disclosures cover the **Senate only** — 100 of 535 members. The House
  publishes its periodic transaction reports as PDFs, which would mean a PDF dependency
  and OCR for the paper filings; its index names who filed but never what they bought.
  Amounts are brackets rather than sizes, and the filing can lag the trade by 45 days,
  so everything measured anchors on the filing date. See
  [docs/congressional-disclosures.md](docs/congressional-disclosures.md).
* Propagation along a disclosed link inherits every limit of the disclosure it walks.
  The percentage is a share of the counterparty's **whole company** in the year of its
  filing, while the shock is usually one segment of the hub — nothing in either filing
  says the counterparty serves that segment, and making the join is the reader's job,
  not the scanner's. Magnitude only travels in the direction the filer wrote it, so
  only counterparty-disclosed edges are walked: a hub naming a supplier tells you what
  the supplier is worth to the hub, never the reverse. And "estimates have not moved"
  has three causes — nobody is looking, everybody looked and judged it immaterial, or
  the revenue has already been re-sourced — of which only the first is an opportunity.
  Events are also anchored on the scan date rather than on the day the hub moved, so
  this family's measured base rate depends partly on how promptly the scanner is run;
  that one is a fix rather than a limit, and the doc ranks it against the rest of the
  future work — including the two things that look worth automating and are not.
  See [docs/link-propagation.md](docs/link-propagation.md).
* Pair dislocation restricts the search and does not remove the statistics. Engle-Granger
  is a weak, asymmetric test, run with the anchor as the dependent variable; at the
  p-value ceiling one in ten unrelated pairs still passes, which is why
  `extra.pairs_tested` and `extra.expected_false_cointegrations` are returned. Both
  states describe the **spread**, not the business: a spread that no longer cointegrates
  is what a relationship that genuinely ended looks like and also what a large lag looks
  like, and the filing that admitted the pair — as old as its date — is the only thing
  on the card that can tell them apart. See
  [docs/pair-dislocation.md](docs/pair-dislocation.md).
* The 13F flow screen is always a quarter behind — the filing deadline is 45 days after
  quarter end and SEC publishes the data set a fortnight later — and it divides by the US
  tape only, so a Canadian or Israeli dual listing's days of volume is overstated and the
  row says so. It reads long positions only, and it cannot net a sub-adviser and its
  parent both reporting the same shares; the per-filer attribution on the row is what
  lets a reader see one book move. The read-through's cluster is the peer group and no
  more coherent than that, and sharing a line is not sharing an exposure — the exposure
  share is on the row for that reason. See [docs/flagged.md](docs/flagged.md).
* The options greeks are computed, not fetched — no free source publishes them. The
  model is Black-Scholes-Merton: European exercise on American contracts, so a put
  (or a high-dividend call) is understated by the early-exercise premium. Yahoo's
  per-contract implied vols are used as given by default and are frequently junk on
  illiquid strikes; `iv_source=solved` re-derives the vol from the quote mid and
  honestly returns null where no Black-Scholes vol reproduces the price. See
  [docs/options-greeks.md](docs/options-greeks.md).
* Live prices are as licensed as their source. Yahoo's streamer is real-time and
  key-free but undocumented, carries no bid/ask on most names, and can change without
  notice like every other yfinance call. Alpaca's free IEX feed is licensed and does
  carry a quote, but IEX is one venue — a few percent of volume, not the consolidated
  tape — and nothing free carries depth of book, so there is no Level 2 here and the
  UI does not pretend otherwise. See [docs/live-streaming.md](docs/live-streaming.md).
* Change detection sees what the filings let it see. The risk-factor and concentration
  diffs need two annual reports on EDGAR, so a company's first 10-K has nothing to
  compare against; the paragraph matcher is tuned on real filings but a rewrite of a
  whole Item 1A still produces rows, labelled `rewrite_suspected` and scored down. Most
  first-appearance concepts are the FASB moving rather than the company, which is why a
  curated `watched` set is named and the rest merely counted. The market screens read
  SEC's calendar frames, which pair an off-calendar filer's balance and flow from
  different fiscal periods; those filers are dropped rather than mis-compared, and the
  per-symbol scan reads them. The rating flag is the one row in the section with no
  filing behind it — Yahoo's action feed is mostly "maintains", so the consensus mix is
  read alongside — and it carries no conventional reading, since a wave of downgrades
  is a short setup and a capitulation bottom in equal measure. See
  [docs/flagged.md](docs/flagged.md).

## Licence

[MIT](LICENSE) — use it, change it, ship it, keep the copyright notice.

The dependencies carry their own licences, and the data providers each set their own
terms of use; neither is granted by this one.

## Disclaimer

Data comes from public and free sources subject to their own availability, rate limits
and terms of use. This project is for research and education and is **not** investment
advice.
