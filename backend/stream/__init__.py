"""Live quote streaming.

The rest of the platform is request/response: a command runs, a frame comes
back. Prices are the one thing worth pushing instead of polling, so this
package holds a small fan-out hub — one upstream websocket per provider, any
number of downstream subscribers — and the sources that feed it.

Two sources, chosen per request with ``provider=``:

``yahoo``
    Yahoo Finance's public streamer. Key-free, real-time last price for US
    stocks, ETFs, indices, futures, FX and crypto. No bid/ask, no licence,
    no SLA — the same footing as every other yfinance call in the stack.

``alpaca``
    Alpaca Markets' data websocket, on the free IEX feed. Needs an app key
    (free, no funded account) and returns licensed trades *and* NBBO-style
    quotes, so it is the source to pick when bid/ask matters. Only US stocks
    and ETFs.

The hub is process-wide and lazy: nothing connects until the first
subscriber arrives, and the upstream socket closes shortly after the last one
leaves.
"""
from __future__ import annotations

from .hub import StreamHub, Subscription, Tick, available_providers, get_hub, shutdown_all

__all__ = ["StreamHub", "Subscription", "Tick", "available_providers", "get_hub", "shutdown_all"]
