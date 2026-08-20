"""Paper trading: one strategy interface, two feeds.

The property this package is designed around: **the exact same strategy code
runs against historical bars and against the live stream**, through the same
context, the same risk gate and the same order machine. What differs is only
the feed —

* ``engine.replay(...)`` drives a strategy with historical bars (an
  event-driven simulation with next-open fills and slippage), and
* ``manager.LiveSession`` drives the identical object with ticks from the
  stream hub, filling paper orders against real quotes as they print.

There is no broker and no real money anywhere in this package: the OMS is a
paper book with explicit order states, and the risk gate in front of it
exists so the *shape* of live trading — limits, rejections, a kill switch —
is practiced where mistakes are free.

Layout::

    strategy.py    Strategy base class + the Context handed to every hook
    oms.py         PaperOMS — order lifecycle, positions, cash, P&L
    risk.py        RiskGate — limits, stale-data check, the kill switch
    engine.py      PaperEngine (feed-agnostic core) + replay()
    strategies.py  built-in strategies + the (gated) custom-code loader
    manager.py     one live session per user, on the server's event loop
"""
from __future__ import annotations

from .engine import PaperEngine, replay
from .manager import manager
from .oms import PaperOMS
from .risk import RiskGate
from .strategy import Context, Strategy

__all__ = ["Context", "PaperEngine", "PaperOMS", "RiskGate", "Strategy", "manager", "replay"]
