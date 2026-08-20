# The Python playground

Status: implemented 2026-08-19. `backend/playground/` (kernel and manager),
`backend/routers/playground.py`, the **Playground** view in `frontend/`,
tested in `tests/test_playground.py`.

---

Everything else in the terminal is a fixed surface: commands with named
parameters, panels with fixed columns. The playground is the escape hatch —
real Python against the same data layer, in the browser, with state that
survives between runs. Load a dataset once, then model it ten different ways:
quant research, backtesting sketches, ML, NLP over the newswire, statistics
on the live tape.

## What is in the namespace

| Name | What it is |
|---|---|
| `mft` | the whole command surface — `mft.equity.price.historical(...).to_df()`, all 319 commands |
| `np`, `pd` | numpy and pandas, pre-imported |
| `show(obj)` | render a DataFrame/Series as a table in the output panel (anything else as `repr`) |
| `chart(df)` / `chart(y1, y2, x=…, labels=…)` | draw a line chart in the output panel |
| `live_ticks("BTC-USD,SPY", seconds=10)` | **real-time**: collect every print from Yahoo's live stream for the window, as a DataFrame — symbol, price, change, volume, time, exchange |
| `tick_db()` | a DuckDB connection over the recorded Parquet tick store, view `ticks` pre-registered — `tick_db().sql("select …").df()` scans the whole tape columnar |
| by import | scipy, statsmodels, scikit-learn, and anything else installed in the server's environment |

A trailing bare expression auto-displays, notebook-style: `df.tail()` as the
last line renders a table without `show()`. Prints come back as stdout;
errors come back as tracebacks that start at your code, not the kernel's.

## The kernel

Each user gets one kernel — a `python -m backend.playground.kernel`
subprocess, started on the first run, holding the namespace between runs (the
variables strip above the output shows what is defined). Runs are serialised
per kernel; two tabs cannot interleave. A run that exceeds the timeout
(`MFT_PLAYGROUND_TIMEOUT_SECONDS`, default 120 s) gets the kernel killed and
restarted — the honest cost of an infinite loop is losing your variables, and
the response says so. **Reset kernel** does the same on purpose. Kernels idle
half an hour are reaped.

The pipe protocol survives anything user code prints: the kernel claims its
protocol stream at startup and points fd 1 at `/dev/null`, so even a C
extension writing to stdout cannot corrupt a response.

## Real-time examples that ship in the UI

The Examples menu inserts working starting points: live-tape statistics over
`live_ticks`, a vectorised 12-1 momentum backtest, a scikit-learn
direction-of-tomorrow classifier with honest out-of-sample folds, TF-IDF +
KMeans clustering of the current newswire, and an ADF stationarity test on a
pair spread. Each runs as inserted.

## Security, stated plainly

The playground executes **arbitrary Python as the server's own user** — full
filesystem, full network, the terminal's own database. On your own machine
that is precisely the feature. On an internet-reachable deployment it is a
remote-code-execution service with a login page, which is why it follows the
registration switch's shape:

* `MFT_DEBUG=true` (local work): playground on.
* `MFT_DEBUG=false` (a deployment): playground **off** unless
  `MFT_PLAYGROUND_ENABLED=true` is set deliberately — and then every account
  on the host can run code, so close registration first.

There is no sandbox, and none is pretended. A run is bounded in wall-clock
time and output size (100k characters of stdout, 200 table rows, 5k chart
points per series), not in what it may touch.

## Limits worth knowing

* **State lives in one process.** A server restart, a timeout kill, or the
  idle reaper loses the namespace. Anything worth keeping should be written
  to a file or re-derivable from the draft (which stays saved in the browser).
* **`live_ticks` blocks for its whole window** and counts against the run
  timeout; a 60-second collection inside a 120-second budget leaves 60 for
  the model.
* **No matplotlib.** `chart()` covers line charts; anything fancier, print
  the numbers. Adding a plotting dependency for one panel was judged not
  worth it — the terminal draws with Chart.js everywhere else too.
* **The kernel imports the backend**, so a code change to a provider needs a
  kernel reset to be picked up.
