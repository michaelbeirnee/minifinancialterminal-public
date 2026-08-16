"""The calibration loop's clock: a background sweep that grades what it can.

:func:`backend.thesis.memory.grade_pending` is incremental and idempotent, but
it only teaches the engine anything if something actually calls it. Left to a
manual endpoint it never runs, and every base rate downstream stays empty
forever — the log fills with events whose outcomes are never stamped.

So the app owns a clock. One task, one sweep per interval, each grading only
the horizons that have genuinely elapsed since the last pass. The work is
blocking (it downloads prices), so it runs in a worker thread rather than on
the event loop, and every failure is logged and swallowed: a rate-limited
price API must not take the server down with it.

Set ``MFT_GRADING_INTERVAL_HOURS=0`` to switch the clock off and drive grading
by hand through ``POST /api/theses/signals/grade`` instead.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..config import settings

log = logging.getLogger(__name__)

#: Grace period before the first sweep. Long enough that a short-lived process
#: (a script, a smoke test) exits without ever touching the network, short
#: enough that a real server starts grading soon after boot rather than waiting
#: out a full interval it may never reach if it restarts daily.
FIRST_SWEEP_DELAY_SECONDS = 60.0


async def _sweep_forever(interval_seconds: float, batch: int) -> None:
    delay = FIRST_SWEEP_DELAY_SECONDS
    while True:
        await asyncio.sleep(delay)
        delay = interval_seconds
        try:
            from . import memory

            result = await asyncio.to_thread(memory.grade_pending, batch)
            if result.get("graded"):
                log.info("Signal grading swept: %s", result)
            elif result.get("error"):
                log.warning("Signal grading sweep errored: %s", result["error"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the clock outlives its sweeps
            log.warning("Signal grading sweep failed: %s", exc)


def start() -> Optional[asyncio.Task]:
    """Launch the sweep, or return ``None`` if grading is switched off."""
    hours = float(settings.grading_interval_hours)
    if hours <= 0:
        log.info("Automatic signal grading is disabled; grade by hand instead")
        return None
    log.info("Signal grading sweep every %.1fh", hours)
    return asyncio.create_task(
        _sweep_forever(hours * 3600.0, int(settings.grading_batch_size)),
        name="signal-grading",
    )


async def stop(task: Optional[asyncio.Task]) -> None:
    """Cancel the sweep and wait for it to unwind, so shutdown stays clean."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
