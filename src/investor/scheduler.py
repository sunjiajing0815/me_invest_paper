"""APScheduler bootstrap for Phase 0.

Phase 0: one-off DateTrigger runs 30s after startup to validate wiring.
Phase 1: replace DateTrigger with CronTrigger for recurring daily schedule.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger(__name__)


def make_scheduler(sync_job_func: Callable[[], None]) -> BackgroundScheduler:
    """Create and configure the scheduler. Does not start it."""
    sched = BackgroundScheduler(timezone="America/New_York")
    run_at = datetime.now(UTC) + timedelta(seconds=30)
    sched.add_job(
        sync_job_func,
        trigger=DateTrigger(run_date=run_at),
        id="phase0_initial_sync",
        replace_existing=True,
    )
    logger.info(
        "APScheduler created. Initial sync scheduled for %s UTC",
        run_at.strftime("%H:%M:%S"),
    )
    return sched
