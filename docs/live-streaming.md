# Live prices

Status: implemented 2026-08-18. `backend/stream/` (the hub and the two sources),
`backend/routers/stream.py` (the SSE endpoint), `backend/extensions/live.py`
(the command form, `/equity/price/live`), the `Live` module and the **Quote
monitor** box in `frontend/app.js`; tested in `tests/test_stream.py`.

---

Everything else in the terminal is request/response: a command runs, a frame
comes back, and a "quote" is whatever Yahoo's REST payload said when it was
asked. Prices are the one thing worth pushing rather than polling, and they
are also the one thing every commercial terminal charges for. This is what a
key-free stack can do about that, and where it stops.

## The ladder

| Tier | Source | Cost | What it gives | What it lacks |
|---|---|---|---|---|
| default | **Yahoo Finance streamer** | nothing — no key, no account | real-time last price, change, day range and volume for US stocks, ETFs, indices, futures, FX and crypto; prints continue after the close | bid/ask on almost every name; a licence; an SLA |
| optional | **Alpaca Markets** (IEX feed) | a free app key, no funded account | licensed trades **and** bid/ask with sizes for US stocks and ETFs | anything that is not a US-listed stock; the IEX feed is one venue's prints (a few percent of volume) and its quote is not the consolidated NBBO |
| not here | Nasdaq TotalView / SIP | licensed, paid | consolidated tape, full depth of book | — |

Nothing free carries the consolidated tape or depth, so the terminal has no
Level 2 and does not pretend to. Alpaca's paid `sip` feed slots into the same
source (`MFT_ALPACA_FEED=sip`) for anyone who buys it.

## How it is built

**One upstream socket per provider, any number of downstream readers.** A
`StreamHub` owns a `Source`. It reference-counts symbols, so the upstream
subscription is always the union of what somebody is watching and nothing
more; keeps the latest tick per symbol, so a late joiner is handed a price
before the next print; and coalesces per reader, so a slow client receives
the newest tick for each symbol rather than a backlog. The hub is lazy — the
first subscriber opens the socket, and it closes twenty seconds after the last
one leaves — so an idle server holds no upstream connection.

**Server-Sent Events, not a websocket, to the browser.** The UI already talks
to this API with a bearer header on `fetch`, which `EventSource` cannot send
but a streamed `fetch` can, and a one-way price feed needs nothing a websocket
adds. `GET /api/stream/quotes?symbols=AAPL,SPY[&provider=alpaca]` writes
`hello`, then `status` whenever the upstream connects or drops, then `ticks`
frames, with a `: ping` comment every fifteen seconds of silence so proxies
keep the response open. Symbol lists are validated against the terminal's own
ticker vocabulary (`BRK-B`, `^GSPC`, `ES=F`, `EURUSD=X`) and capped at a
hundred.

**Ticks are one shape regardless of source.** `price`, `change`,
`change_percent` (a fraction, like `/equity/price/quote`), `bid`, `ask`,
`bid_size`, `ask_size`, `size`, `volume`, `time`, `exchange`,
`market_hours`, `kind` (`trade` or `quote`), `provider`. Yahoo's percent units
are converted, its int64s (which protobuf serialises as strings) are cast, and
its market-hours enum is spelled out. Alpaca's `BRK.B` is mapped back to the
`BRK-B` the caller asked for. Fields a vendor cannot supply are absent, and the
hub carries the last known value forward, so an Alpaca quote-only message
still arrives with the last trade price on it.

**Reconnect is per source, with capped backoff.** Yahoo re-sends its
subscription every fifteen seconds because the streamer drops one that is
not refreshed. Alpaca stops retrying on `402 auth failed` and `409
insufficient subscription` — no amount of waiting fixes a bad key — and says
so in `/api/stream/status`, which the browser's topbar pill reads: **LIVE ·
YAHOO** green, **CONNECTING** blinking, **RECONNECTING** red with the reason on
hover, **IDLE** when no live view is open.

**The browser holds one connection per provider.** The `Live` module in
`app.js` takes `watch(symbols, onTick, {scope, provider})` registrations from
views and computes the union; a view's watchers are parked when the view is
not the active one (the ticker tape's are scope `global` and stream
everywhere), so the Markets board's twelve tiles, the workspace's boxes and
the tape share one socket. Cells flash green or red as prints move.

## What is live in the UI

* the ticker tape and the watchlist cards on **Markets**;
* the twelve index and asset tiles on **Markets** (`as of` becomes `live ·`);
* the workspace **Price chart** hero, **Watchlist** rows and **Major markets**
  tiles;
* the new **Quote monitor** workspace box — a streaming grid of any tickers you
  name (last, change, change %, volume, time; bid/ask and print size when its
  source is Alpaca), which never calls the delayed quote endpoint at all.

## The command form

`/equity/price/live` (`mft.equity.price.live(symbol="AAPL,SPY")`,
`GET /api/v1/equity/price/live?symbol=…`) is the same feed reached the way
every other command is reached, for callers that want a snapshot rather than
a subscription. `provider=yahoo` opens the streamer, listens for `wait`
seconds (default 3, ceiling 10) and returns the last print per symbol; a
symbol that does not print in the window — every equity, outside market
hours — falls back to the delayed quote and says `source: quote` on the row.
`provider=alpaca` returns Alpaca's snapshot with bid and ask and needs the
key. `/api/stream/snapshot?symbols=…` returns whatever the hub already holds
without opening anything.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MFT_ALPACA_API_KEY`, `MFT_ALPACA_API_SECRET` | unset | Free at [alpaca.markets](https://alpaca.markets); no funded account. Switches the Alpaca source on. |
| `MFT_ALPACA_FEED` | `iex` | `iex` (free), `sip` (paid), `delayed_sip` (free, 15-minute), `test` (a fake ticker, for wiring). |
| `MFT_STREAM_DEFAULT_PROVIDER` | `yahoo` | Which source serves a request that names none. `alpaca` is honoured only once its keys are set. |

## Limits worth knowing

* **Yahoo's streamer is undocumented.** It is the same feed the finance.yahoo.com
  page uses and has been stable for years, but it can change or vanish with no
  notice, exactly like every other yfinance call in the stack. Prices carry no
  licence for redistribution; this is a research tool for the person running it.
* **Alpaca allows one connection per account** on the free plan; the hub
  guarantees this server opens only one, but a second copy of the terminal on
  the same key will fight it.
* **The `live` command's fallback is honest and slow.** Off-hours it waits the
  full window before conceding; pass `wait=1` when you already know the market
  is closed.
* **`python.org` builds of Python on macOS ship without system CAs.** The
  sources build their TLS context from `certifi` for that reason; the Docker
  image does not have the problem.
