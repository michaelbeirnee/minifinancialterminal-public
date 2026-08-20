"""The scheduler entrypoint for the daily production cycle.

Run after the US close (information cutoff today, execution tomorrow):

    30 16 * * 1-5  cd /path/to/repo && .venv/bin/python -m cli.daily_cycle --capture

The morning-after reconcile (ingest fills, compare positions):

    45 9 * * 1-5   cd /path/to/repo && .venv/bin/python -m cli.daily_cycle --reconcile-only

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
    parser.add_argument("--capture", action="store_true",
                        help="capture today's estimate/option/crowding snapshots first")
    parser.add_argument("--as-of", default=None, help="information cutoff date (default: latest bar)")
    parser.add_argument("--reconcile-only", action="store_true",
                        help="only ingest fills and compare positions; no new targets")
    args = parser.parse_args(argv)

    from backend.database import init_db, SessionLocal
    from backend.trading.production import reconcile, run_daily_cycle

    init_db()
    db = SessionLocal()
    try:
        if args.reconcile_only:
            result = reconcile(db, broker_kind=args.broker)
            print(json.dumps(result, indent=2, default=str))
            return 1 if result["discrepancies"] else 0
        run = run_daily_cycle(
            db,
            orders_enabled=args.orders,
            broker_kind=args.broker,
            capture_snapshots=args.capture,
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
