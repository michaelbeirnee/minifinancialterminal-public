"""The playground kernel — the child process that owns the namespace.

Run as ``python -m backend.playground.kernel``. Speaks newline-delimited JSON:
one request object per line on stdin, one response object per line on the
*protocol* stream. The protocol stream is a dup of the original stdout taken
at startup, after which fd 1 is pointed at ``/dev/null`` — so user code (and
any C extension) can print whatever it likes without corrupting the pipe;
Python-level ``print`` is additionally captured per request and returned as a
``stdout`` output.

Request:  ``{"id": 1, "code": "..."}``
Response: ``{"id": 1, "ok": true, "outputs": [...], "elapsed": 0.12,
             "variables": ["df", "model"]}``

Output items, in the order produced:
    {"type": "stdout",  "text": ...}
    {"type": "error",   "text": traceback}
    {"type": "table",   "columns": [...], "rows": [[...]], "note": "..."}
    {"type": "chart",   "x": [...], "series": [{"label": ..., "data": [...]}]}
    {"type": "repr",    "text": ...}

The namespace persists across requests — that is the whole point. If the last
statement of a request is a bare expression its value is auto-displayed,
DataFrames and Series as tables, everything else as ``repr``.
"""
from __future__ import annotations

import ast
import io
import json
import math
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

MAX_STDOUT = 100_000          # characters of print output returned per run
MAX_TABLE_ROWS = 200
MAX_CHART_POINTS = 5_000
MAX_REPR = 20_000


# --------------------------------------------------------------------------- #
# Output collection
# --------------------------------------------------------------------------- #
_outputs: List[Dict[str, Any]] = []


def _json_safe(v: Any) -> Any:
    """One cell, made JSON-clean. NaN/inf become null; oddballs become str."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    try:
        import numpy as np

        if isinstance(v, np.integer):
            return int(v)
        if isinstance(v, np.floating):
            f = float(v)
            return f if math.isfinite(f) else None
        if isinstance(v, np.bool_):
            return bool(v)
    except ImportError:  # pragma: no cover - numpy is a hard dependency
        pass
    return str(v)


def _frame_to_table(df: Any, note: str = "") -> Dict[str, Any]:
    import pandas as pd

    if isinstance(df, pd.Series):
        df = df.to_frame(df.name if df.name is not None else "value")
    total = len(df)
    shown = df.head(MAX_TABLE_ROWS)
    # A meaningful index (dates, symbols) belongs in the table; a default
    # RangeIndex is just row numbers and stays out.
    if not isinstance(shown.index, pd.RangeIndex):
        shown = shown.reset_index()
    columns = [str(c) for c in shown.columns]
    rows = [[_json_safe(v) for v in row] for row in shown.itertuples(index=False, name=None)]
    if total > MAX_TABLE_ROWS:
        note = (note + " " if note else "") + "showing {} of {} rows".format(MAX_TABLE_ROWS, total)
    return {"type": "table", "columns": columns, "rows": rows, "note": note.strip()}


def show(obj: Any, note: str = "") -> None:
    """Render ``obj`` in the output panel — DataFrames/Series as a table."""
    import pandas as pd

    if isinstance(obj, (pd.DataFrame, pd.Series)):
        _outputs.append(_frame_to_table(obj, note))
    else:
        _outputs.append({"type": "repr", "text": repr(obj)[:MAX_REPR]})


def chart(*series: Any, x: Any = None, labels: Any = None, title: str = "") -> None:
    """Draw a line chart.

    ``chart(df)`` plots every numeric column against the index;
    ``chart(y)`` or ``chart(y1, y2, ...)`` plots arrays, optionally against
    ``x=``, named by ``labels=``.
    """
    import numpy as np
    import pandas as pd

    out_series: List[Dict[str, Any]] = []
    xs: Optional[List[Any]] = None
    if len(series) == 1 and isinstance(series[0], pd.DataFrame):
        df = series[0]
        num = df.select_dtypes(include=[np.number])
        if num.empty:
            raise ValueError("chart(df): no numeric columns to plot")
        xs = [_json_safe(v) for v in num.index[:MAX_CHART_POINTS]]
        for col in num.columns:
            out_series.append({"label": str(col),
                               "data": [_json_safe(v) for v in num[col].tolist()[:MAX_CHART_POINTS]]})
    else:
        names = list(labels) if labels is not None else []
        for i, s in enumerate(series):
            if isinstance(s, pd.Series):
                if xs is None:
                    xs = [_json_safe(v) for v in s.index[:MAX_CHART_POINTS]]
                label = names[i] if i < len(names) else (str(s.name) if s.name is not None else "series {}".format(i + 1))
                data = s.tolist()
            else:
                label = names[i] if i < len(names) else "series {}".format(i + 1)
                data = list(s)
            out_series.append({"label": label,
                               "data": [_json_safe(v) for v in data[:MAX_CHART_POINTS]]})
        if x is not None:
            xs = [_json_safe(v) for v in list(x)[:MAX_CHART_POINTS]]
    if not out_series:
        raise ValueError("chart() needs at least one series")
    if xs is None:
        xs = list(range(len(out_series[0]["data"])))
    _outputs.append({"type": "chart", "x": xs, "series": out_series, "title": title})


def live_ticks(symbols: str, seconds: float = 10.0):
    """Collect real-time prints from Yahoo's streamer into a DataFrame.

    Every tick in the window, not just the last per symbol — this is the raw
    material for microstructure toys, realised-vol-in-the-small, tape stats.
    Columns: symbol, price, change_percent, volume, time (UTC), exchange.
    FX and crypto print around the clock; equities only while the market is
    open. Blocks for the full ``seconds``.
    """
    import pandas as pd

    from backend.stream.hub import normalise_symbols
    from backend.stream.sources import YAHOO_URL, _ssl_context, decode_yahoo
    from websockets.sync.client import connect

    syms = normalise_symbols([symbols])
    seconds = max(1.0, min(float(seconds), 120.0))
    rows: List[Dict[str, Any]] = []
    deadline = time.monotonic() + seconds
    with connect(YAHOO_URL, ssl=_ssl_context(), open_timeout=10) as ws:
        ws.send(json.dumps({"subscribe": syms}))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = ws.recv(timeout=remaining)
            except TimeoutError:
                break
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if frame.get("type") != "pricing":
                continue
            try:
                tick = decode_yahoo(frame.get("message", ""))
            except Exception:  # noqa: BLE001 - one undecodable frame
                continue
            if tick.get("symbol") in syms and tick.get("price") is not None:
                rows.append({k: tick.get(k) for k in
                             ("symbol", "price", "change_percent", "volume", "time", "exchange")})
    df = pd.DataFrame(rows, columns=["symbol", "price", "change_percent", "volume", "time", "exchange"])
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"])
    return df


# --------------------------------------------------------------------------- #
# The namespace
# --------------------------------------------------------------------------- #
def tick_db():
    """A DuckDB connection over the recorded tick store, view ``ticks`` ready.

    ``tick_db().sql("select symbol, count(*) from ticks group by 1").df()`` —
    the whole store, scanned columnar, never loaded wholesale. Raises if
    nothing has been recorded yet.
    """
    from backend.stream.tickdb import connect

    return connect()


def build_namespace() -> Dict[str, Any]:
    import numpy as np
    import pandas as pd

    import backend.extensions  # noqa: F401 - registers every command
    from backend.core.interface import mft

    ns: Dict[str, Any] = {
        "__name__": "__playground__",
        "mft": mft,
        "np": np,
        "pd": pd,
        "show": show,
        "chart": chart,
        "live_ticks": live_ticks,
        "tick_db": tick_db,
    }
    return ns


_HIDDEN = None  # set after build: the initial names, hidden from the variable list


def _variables(ns: Dict[str, Any]) -> List[str]:
    return sorted(k for k in ns if k not in _HIDDEN and not k.startswith("_"))


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def run_code(code: str, ns: Dict[str, Any]) -> Dict[str, Any]:
    """Execute ``code`` in ``ns``; auto-display a trailing bare expression."""
    global _outputs
    _outputs = []
    started = time.monotonic()
    captured = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = captured
    ok = True
    try:
        tree = ast.parse(code, mode="exec")
        tail_expr = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            tail_expr = ast.Expression(tree.body.pop().value)
        if tree.body:
            exec(compile(tree, "<playground>", "exec"), ns)  # noqa: S102 - the feature
        if tail_expr is not None:
            value = eval(compile(tail_expr, "<playground>", "eval"), ns)  # noqa: S307
            if value is not None:
                show(value)
    except Exception:  # noqa: BLE001 - reported, never fatal to the kernel
        ok = False
        # Drop the run_code frames; the user's traceback starts at <playground>.
        tb = traceback.format_exc()
        lines = tb.splitlines()
        start = next((i for i, l in enumerate(lines)
                      if l.lstrip().startswith('File "<playground>"')), 1)
        _outputs.append({"type": "error", "text": "\n".join([lines[0]] + lines[start:])[:MAX_REPR]})
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    printed = captured.getvalue()
    outputs = list(_outputs)
    if printed:
        text = printed if len(printed) <= MAX_STDOUT else (
            printed[:MAX_STDOUT] + "\n… output truncated at {} characters".format(MAX_STDOUT))
        outputs.insert(0, {"type": "stdout", "text": text})
    return {
        "ok": ok,
        "outputs": outputs,
        "elapsed": round(time.monotonic() - started, 4),
        "variables": _variables(ns),
    }


def main() -> None:  # pragma: no cover - exercised as a real subprocess
    global _HIDDEN
    # Claim the pipe, then silence fd 1 so nothing user code does can touch it.
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)

    ns = build_namespace()
    _HIDDEN = set(ns)
    proto.write(json.dumps({"ready": True, "pid": os.getpid()}) + "\n")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if req.get("op") == "shutdown":
            break
        result = run_code(str(req.get("code", "")), ns)
        result["id"] = req.get("id")
        try:
            proto.write(json.dumps(result) + "\n")
        except (TypeError, ValueError):
            # A pathological repr slipped through; report that rather than dying.
            proto.write(json.dumps({
                "id": req.get("id"), "ok": False, "elapsed": result.get("elapsed"),
                "variables": result.get("variables", []),
                "outputs": [{"type": "error",
                             "text": "Output could not be serialised to JSON."}],
            }) + "\n")


if __name__ == "__main__":
    main()
