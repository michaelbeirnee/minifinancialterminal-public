# Paper trading and the tick recorder

Status: implemented 2026-08-19. `backend/trading/` (strategy interface, OMS,
risk gate, engine, live sessions), `backend/stream/recorder.py` (the Parquet
tick store), `backend/routers/trading.py`, recorder endpoints in
`backend/routers/stream.py`, `/equity/price/ticks` in
`backend/extensions/live.py`, the **Paper trading** view in `frontend/`;
tested in `tests/test_trading.py` and `tests/test_recorder.py`.

---

Two pieces, built to compound: a **recorder** that writes down the live tape
(free real-time data is ephemeral — nobody sells you yesterday's free ticks),
and a **paper trading loop** designed around one property:

> The exact same strategy code runs against historical bars and against the
> live stream — through the same context, the same risk gate and the same
> order machine.

That property is what kills the "backtest works, production doesn't" class of
bug, and it is tested directly: the parity test feeds identical prices down
both paths and asserts identical fills.

## The strategy interface

```python
class Momentum(Strategy):
    params = {"fast": 10, "slow": 30}

    def on_start(self, ctx): ...
    def on_tick(self, ctx, tick): ...     # every live print (replay: bar opens)
    def on_bar(self, ctx, bar): ...       # completed bars (live: built from ticks)
    def on_fill(self, ctx, fill): ...     # its own executions
    def on_timer(self, ctx, ts): ...      # live wall clock
    def on_stop(self, ctx): ...
```

Everything a strategy may do goes through `ctx`: `ctx.buy/sell/order` (which
pass the risk gate), `ctx.position`, `ctx.cash`, `ctx.equity`, `ctx.last`,
`ctx.log`. Three built-ins ship as copyable examples — `buy_and_hold`,
`sma_cross`, `tick_reversion` — and custom code (a `Strategy` subclass) is
accepted where the playground switch allows it, because it is the same
decision: your Python on your server.

## The order machine

Explicit states, and a position changes **only** on a fill:

```
CREATED -> SUBMITTED -> ACKNOWLEDGED -> FILLED
                             +-> CANCELLED        (risk refusal -> REJECTED)
```

An order placed in a hook rests and fills against the **next** event — the
next tick live, the next bar's *open* in replay. Nothing fills at the price
that triggered it. Fills cross the spread when the tick carries bid/ask
(Alpaca), otherwise last ± `slippage_bps`; limit orders rest until
marketable; average cost, realised P&L and crossing through flat are all
accounted. Partial fills are not modelled in v1 (the state machine carries
`filled_qty`, so they are an extension, not a rewrite).

## The risk gate and the kill switch

Every order passes checks scaled to the book: per-order notional, per-symbol
position, gross exposure, an open-order cap, a stale-data refusal (no order
against a quote older than `stale_seconds`), and a loss floor that does not
just refuse the order — it **engages the kill switch**. The switch is
one-way: no further strategy order passes, open orders are cancelled, and on
request the book is flattened at the last known price (the one path allowed
to bypass the size checks). Rejections are counted by reason and shown, so a
strategy walking into the same wall is visible.

## Execution: internal fills, or a real paper venue

The engine owns the book either way; an **executor** decides how an
acknowledged order becomes a fill.

* `internal` (default, zero keys) — orders rest in the OMS and fill against
  the next tick the engine sees.
* `alpaca` — orders route to **Alpaca's paper-trading API**, so fills come
  from a real venue's simulation: their matching, their slippage, an account
  that exists outside this process. Orders are queued and flushed off the
  event loop; fills are polled back and booked into the local OMS, which
  stays the reconciliation record — what was requested, what the venue
  acknowledged, what actually filled — with the broker's own account equity
  and positions shown alongside so drift is visible. Uses the same
  `MFT_ALPACA_API_KEY`/`SECRET` as the data feed (use a *paper* account's
  keys). US stocks and ETFs only; the kill switch cancels at the venue first
  and flattens through it.

One hard line, enforced in code rather than configuration: the adapter
refuses any host other than `paper-api.alpaca.markets`. This project places
no real-money orders.

## Live sessions and replay

`POST /api/trading/paper/start` subscribes a session to the stream hub
(Yahoo key-free, or Alpaca for bid/ask fills), builds N-second bars from
ticks on the wall clock, and snapshots equity as it goes — one session per
user, in-memory and ephemeral like a playground kernel, killed cleanly on
stop or server shutdown. `POST /api/trading/replay` runs the same strategy
over `/equity/price/historical` bars (daily or intraday) — or, with
`source="ticks"`, over **bars built from your own recorded tape** at any
width down to one second, which is the honest backtest for tick-driven
strategies — and returns metrics (return, max drawdown, fills, round trips,
win rate), the equity curve, and the annotated fills. The **Paper trading** view drives both and carries the
red button.

## The tick recorder

A hub subscriber that appends every tick it sees to date-partitioned Parquet:

```
tick_store/date=2026-08-19/yahoo-143022-514301.parquet
```

Buffered in memory, flushed every 5,000 ticks or 60 seconds and again on
stop (pending ticks are swept, so a clean stop loses nothing). Start it from
the UI, `POST /api/stream/recorder/start?symbols=…`, or from boot with
`MFT_RECORD_SYMBOLS=SPY,QQQ,BTC-USD`. Read it back with
`/equity/price/ticks?start_date=…` anywhere commands run — and at scale
through **DuckDB**, which scans the Parquet parts directly instead of loading
them: `/equity/price/bars_from_ticks` resamples the tape into OHLC bars of
any width, `/equity/price/tick_stats` reports what each day holds, and the
Playground's `tick_db()` hands back a connection with the whole store
registered as the `ticks` view for free-form SQL. Dates are UTC — the store
partitions by the day the tick was recorded, not your timezone. The store is deliberately **not** under the clearable cache directory:
a tick nobody wrote down is gone.

## Limits worth knowing

* **Paper fills are optimistic.** All-or-nothing at top-of-book (or slipped
  last): no queue position, no market impact, no partial fills, no borrow
  cost on shorts. Paper P&L is an upper bound on live P&L.
* **Sessions are ephemeral** — a restart loses the book, on purpose. A paper
  book that pretended to persist would be pretending to be a broker; the
  real portfolio module is next door for positions worth keeping.
* **Integer shares.** The built-ins size in whole shares, so a $25k slice
  cannot buy a $68k coin — the honest outcome, visible in the log.
* **Replay's `on_tick` sees bar opens**, not real intra-bar prints. A
  tick-driven strategy backtests properly over `source="ticks"` bars from the
  recorder — which is one more reason the recorder exists.
* **Alpaca paper fills arrive by polling** (about once a second), so a live
  broker fill lands a beat later than an internal one, and the two books can
  briefly disagree; the broker snapshot on the session panel is there to
  catch real drift, not the polling gap.
* **The loss limit is per session**, not per calendar day, and replay months
  of a drawdown can trip it. That is the gate working; loosen `max_loss` per
  run if it is not what you want.
