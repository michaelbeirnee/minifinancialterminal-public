"""The scheduler entrypoint for the daily production cycle.

The one job that must never be skipped is snapshot capture: archive-first data
(estimates, options, crowding) exists only from the day it was captured, and a
missed day is permanently unrecoverable. Capture therefore runs by default and
has its own mode that needs no research vintage:

    35 16 * * 1-5  cd /path/to/repo && .venv/bin/python -m cli.daily_cycle --capture-only
    45 9  * * 1-5  cd /path/to/repo && .venv/bin/python -m cli.daily_cycle --reconcile-only

Once a vintage is promoted, swap the afternoon line for the full cycle (which
captures first, then builds the target book):

    35 16 * * 1-5  cd /path/to/repo && .venv/bin/python -m cli.daily_cycle

Everything is record-only unless BOTH ``--orders`` is passed and the
MFT_TRADING_ENABLED environment kill switch is on.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one daily production trading cycle.")
    parser.add_argument("--orders", action="store_true",
                        help="request order submission (still needs MFT_TRADING_ENABLED=true)")
    parser.add_argument("--broker", default="ledger", choices=("ledger", "alpaca"))
    parser.add_argument("--no-capture", action="store_true",
                        help="skip today's snapshot capture (capture is on by default)")
    parser.add_argument("--as-of", default=None, help="information cutoff date (default: latest bar)")
    parser.add_argument("--symbols", default=None,
                        help="comma-separated capture universe override (capture-only mode)")
    parser.add_argument("--capture-only", action="store_true",
                        help="archive today's raw+feature snapshots and exit; needs no vintage")
    parser.add_argument("--reconcile-only", action="store_true",
                        help="only ingest fills and compare positions; no new targets")
    args = parser.parse_args(argv)

    from backend.database import init_db, SessionLocal
    from backend.backtest.multisource_research import archive_current_snapshots
    from backend.trading.production import reconcile, resolve_capture_universe, run_daily_cycle

    init_db()
    db = SessionLocal()
    try:
        if args.capture_only:
            symbols = resolve_capture_universe(
                db, (args.symbols or "").split(",") if args.symbols else None
            )
            result = archive_current_snapshots(symbols, db)
            print(json.dumps({
                "as_of": result["as_of"],
                "symbols": len(symbols),
                "feature_rows": len(result["captured"]),
                "raw_rows": result["raw_rows"],
                "rate_limited_symbols": result.get("rate_limited_symbols", []),
                "warnings": result["warnings"][:20],
            }, indent=2, default=str))
            return 0 if result["raw_rows"] or result["captured"] else 1
        if args.reconcile_only:
            result = reconcile(db, broker_kind=args.broker)
            print(json.dumps(result, indent=2, default=str))
            return 1 if result["discrepancies"] else 0
        run = run_daily_cycle(
            db,
            orders_enabled=args.orders,
            broker_kind=args.broker,
            capture_snapshots=not args.no_capture,
            as_of=args.as_of,
        )
        print(json.dumps({
            "run_id": run.id,
            "as_of": run.as_of,
            "status": run.status,
            "nav": run.nav,
            "stages": run.stages,
            "gateway": run.gateway,
        }, indent=2, default=str))
        return 0 if run.status in ("recorded", "submitted") else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
