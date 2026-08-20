"""The Python playground: a persistent per-user kernel for quick research.

Everything else in the terminal is a fixed surface — commands with named
parameters, panels with fixed columns. The playground is the escape hatch:
real Python against the same data layer, with state that survives between
runs so a dataset loaded once can be modelled ten different ways.

Three pieces:

``kernel``
    The child process (``python -m backend.playground.kernel``). Owns the
    namespace — ``mft``, numpy/pandas/scipy/statsmodels/sklearn, ``show()``,
    ``chart()``, ``live_ticks()`` — executes one request at a time, and
    reports structured outputs (stdout, tables, charts, tracebacks) over a
    line-JSON pipe. User prints go to a buffer, never the pipe.

``manager``
    Parent-side bookkeeping: one kernel per user, spawned on first run,
    serialised with a lock, killed on timeout or reset, reaped when idle.

``routers/playground.py``
    ``POST /api/playground/run`` and friends.

Security is by honesty rather than sandboxing: the kernel executes arbitrary
Python as the server's own user. That is the feature — it is the operator's
own machine and the operator's own code. On an internet-reachable deployment
(``MFT_DEBUG=false``) it is **off** unless ``MFT_PLAYGROUND_ENABLED=true`` is
set deliberately, the same shape as the registration switch.
"""
from __future__ import annotations

from .manager import KernelManager, manager

__all__ = ["KernelManager", "manager"]
