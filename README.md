# Mini Financial Terminal (MFT)

**[michaelbeirnee.github.io/minifinancialterminal-public](https://michaelbeirnee.github.io/minifinancialterminal-public/)** — project site

An **open-source financial research terminal** — an OpenBB-style data platform with
**277 commands** across equities, ETFs, crypto, FX, derivatives, macro, fixed income
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
| **Web UI** | `http://localhost:8000` → **DATA** tab browses every command; **PORTFOLIO** tab tracks what you own; **SAVED** tab holds watchlists, alerts, saved commands and history; any stock page has a **FINANCIALS** tab with the three statements — annual, quarterly, or the year so far beside an estimate of where the full year lands — plus the revenue behind them split by segment, geography and product line; an **EXPOSURE** map of who it buys from and sells to; a **COMPARE** tab that builds a peer group out of three disagreeing sources and puts it side by side on valuation, growth, risk and what each company actually sells; **THESIS** tab tracks falsifiable claims and the signals behind them; **MODELING** tab builds savable DCFs seeded from the filings; **ASSISTANT** tab answers questions and runs commands for you |

All four read the same registry, so a command added under `backend/extensions/`
appears in every one of them with no extra wiring.

---

## Command coverage

| Menu | Cmds | What's in it |
|---|---:|---|
| **equity** | 70 | prices, quotes, performance, profile, search, screener, 10 discovery screens, 5 calendars, 20 fundamental commands (including the three statements as one ordered document and revenue split by segment, geography and product line), 5 estimates, 7 ownership, shorts, dark pool, 3 supply-chain relationship commands mined from filings, and a peer group blended from classification, SIC registration and the filings that name the company as competition — with the side-by-side comparison built on it |
| **technical** | 40 | 35+ indicators: MAs (SMA/EMA/WMA/HMA/ZLMA/DEMA/TEMA), RSI, MACD, stochastic, CCI, ADX, Aroon, Ichimoku, Supertrend, PSAR, Bollinger, Keltner, Donchian, OBV, A/D, CMF, MFI, VWAP, Fisher, TSI, PPO, DeMark, vol cones, Hurst, Clenow momentum |
| **economy** | 34 | CPI, PCE, GDP, unemployment, payrolls, claims, money supply, Fed balance sheet, SLOOS, financial conditions, house prices, trade, debt, country profiles, calendars, surveys |
| **quantitative** | 18 | normality battery, unit root, CAPM, rolling stats, Sharpe/Sortino/Calmar/Omega/Ulcer, VaR & CVaR, drawdown |
| **fixedincome** | 15 | Treasury curves, auctions, debt, TIPS, spreads, ICE BofA, Moody's, commercial paper, HQM, SOFR/EFFR/OBFR/IORB, mortgage rates |
| **etf** | 11 | search, profile, holdings, sector & asset-class weights, bond ratings, performance, reverse equity exposure |
| **econometrics** | 10 | correlation/covariance, OLS (+ full summary), VIF, Granger causality, cointegration, unit root, autocorrelation diagnostics, panel models (pooled / fixed / between / first-difference / Fama-MacBeth) |
| **regulators** | 10 | SEC CIK maps, registrant search, EDGAR full-text search, SIC codes, press releases, bulk datasets; CFTC COT |
| **charting** | 9 | candlesticks, comparison, drawdown, histogram, correlation heatmap, yield curve, performance bars, vol cones, and a generic "chart any command" |
| **commodity** | 8 | spot prices, futures, complex performance, COT, EIA petroleum/STEO/gas storage |
| **derivatives** | 8 | option chains, expirations, unusual activity, IV surface, put/call snapshots, futures history and term structure |
| **index** | 7 | membership for 16 indices, index prices, regional snapshots, sector breakdown, 11 long-run S&P 500 valuation series |
| **crypto** | 6 | prices, ranked market table, global dominance, categories, coin universe |
| **thesis** | 12 | candidate funnels for insider and congressional clusters, undervalued large caps, undervalued growth, crowded shorts and one-month price dislocations; issuer-level insider/holder detail; and the graded signal log with per-family base rates |
| **currency** | 4 | pairs, history, ECB reference rates, cross-rate snapshots |
| **news** | 4 | company headlines, merged newswire tape, topic search, feed list |
| **sentiment** | 5 | lexicon-scored news sentiment: market-wide mood, all 11 GICS sectors, per-ticker summaries, story-by-story scores, historical weekly series rebuilt from the Google News archive (feeds the `news_sentiment` backtest strategy — sector ETFs like XLE trade their sector's news) |

`GET /api/v1/_registry` returns the whole surface; `/api/v1/_search?query=…` finds a command.

---

## Data providers — all free

| Provider | Covers | Key |
|---|---|---|
| **Yahoo Finance** (`yfinance`) | prices, fundamentals, options, holders, estimates, screeners, calendars | none |
| **SEC EDGAR** | XBRL fundamentals, segment revenue read from the filings, filings, full-text search, Form 4, 13F, fails-to-deliver, supplier/customer relationships | none |
| **FRED** | US macro, rates, credit spreads (key-free CSV endpoint) | optional |
| **US Treasury / TreasuryDirect / NY Fed** | yield curves, auctions, debt, SOFR/EFFR/OBFR | none |
| **ECB · World Bank · IMF · OECD · Frankfurter** | euro-area curves, FX, cross-country macro, WEO forecasts | none |
| **FINRA · CFTC** | short-sale volume, ATS/dark-pool volume, Commitments of Traders | none |
| **Senate EFD** | STOCK Act periodic transaction reports — congressional trading (Senate only) | none |
| **Stooq · Cboe · Nasdaq · Wikipedia · multpl** | backup prices, delayed option chains, calendars, index membership, CAPE | none |
| **CoinGecko** | crypto prices, market caps, dominance | none |
| **EIA · BLS** | energy reports, labour & price statistics | EIA needs a free key |
| **RSS newswires** | ~40 feeds: WSJ, FT, NYT, Economist, CNBC, MarketWatch, Yahoo, Fortune, Business Insider, Benzinga, TheStreet, Nasdaq, BBC, Guardian, NPR, Fed, ECB, BoE, SEC, OilPrice, CoinDesk, Cointelegraph — plus Reuters, Bloomberg and Barron's via Google News RSS search | none |

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
| **Backtesting** | Two engines sharing one cost model: a fast **vectorized** backtester and an **event-driven** engine (explicit cash, positions, commission, slippage, execution latency, per-fill trade log). Both apply a one-bar execution lag to avoid look-ahead bias. Position-sizing overlays (vol targeting, trailing/fixed stop-losses) compose with any strategy. |
| **Backtest analysis** | Parameter **grid sweeps**, **walk-forward** evaluation (rolling train/purge/test folds, per-fold re-fitting, stitched out-of-sample equity), benchmark-relative **attribution** (alpha/beta, tracking error, information ratio, up/down capture), **cost-sensitivity** ladders and block-bootstrap **Monte Carlo** bands on terminal wealth and drawdown. |
| **Reports** | Tearsheet metrics (CAGR, Sharpe, Sortino, Calmar, max drawdown, win rate) plus a self-contained HTML report with equity and drawdown charts. |
| **Auth** | JWT bearer auth with bcrypt hashing and **revocable sessions** — logout and password changes kill the token server-side. Platform routes require a token unless `MFT_PLATFORM_REQUIRE_AUTH=false`. |
| **Caching** | Graded-TTL cache (memory + disk) in front of every outbound call — 2 min for quotes, a week for reference data. Stats at `/api/system/cache`. |

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
              /thesis/crowded_shorts     — short case or squeeze case; direction-neutral
              /thesis/price_dislocations — large 1m drawdowns that need explaining
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
earnings in the denominator; a crowded short can become either a short or a
squeeze; and a drawdown does not reveal its cause. Each source states its own
failure mode and it lands in the triage prompt as the rule the model must argue
past, so nothing below the funnel needs to know which scanner ran.

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
                 govstats, newsfeeds — one module per data source, no command logic
  extensions/    equity, equity_fundamental, etf, crypto, currency, derivatives,
                 index, news, sentiment, economy, fixedincome, commodity, regulators,
                 technical (+ indicators.py), quantitative, econometrics, charting
  assistant/     prompt.py     system prompt generated from the registry
                 tools.py      the four read-only tools the model can call
                 service.py    the streaming chat loop
  models.py database.py auth.py          SQL schema, engine wiring, JWT + sessions
  routers/       auth, user (saved actions), portfolio, data, factors, backtest,
                 reports, system, assistant
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
| `MFT_ANTHROPIC_API_KEY` | unset | **Paid.** Switches on the Assistant tab and the thesis triage / deep-dive steps. Nothing else in the platform uses it. |
| `MFT_ASSISTANT_MODEL` | `claude-opus-5` | Model backing the assistant. |
| `MFT_ASSISTANT_EFFORT` | `medium` | `low`…`max` — how hard it thinks per reply. |
| `MFT_ASSISTANT_MAX_TOOL_ROUNDS` | `6` | Command calls allowed before it must answer. |
| `MFT_GRADING_INTERVAL_HOURS` | `12` | Hours between signal-grading sweeps. `0` disables the clock. |
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

* Yahoo's endpoints are unofficial and rate-limit under load; commands report which
  provider served them, and fall back where a second free source exists (Stooq for
  prices, SEC for fundamentals).
* `/etf/equity_exposure` scans a fixed ETF universe because no free source publishes a
  reverse holdings index — pass `universe=` to widen it.
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

## Licence

[MIT](LICENSE) — use it, change it, ship it, keep the copyright notice.

The dependencies carry their own licences, and the data providers each set their own
terms of use; neither is granted by this one.

## Disclaimer

Data comes from public and free sources subject to their own availability, rate limits
and terms of use. This project is for research and education and is **not** investment
advice.
