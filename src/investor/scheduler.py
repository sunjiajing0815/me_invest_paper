"""APScheduler bootstrap — Phase 1.

Runs run_daily_report Mon–Fri at 16:15 ET after market close.
misfire_grace_time=1800 means: if the box was offline at 16:15, run within 30 min on next start.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def make_scheduler(daily_report_func: Callable[[], None]) -> BackgroundScheduler:
    """Create and configure the scheduler. Does not start it."""
    sched = BackgroundScheduler(timezone="America/New_York")
    sched.add_job(
        daily_report_func,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=16,
            minute=15,
            timezone="America/New_York",
        ),
        id="daily_report",
        replace_existing=True,
        misfire_grace_time=60 * 30,
    )
    logger.info("APScheduler created. Daily report scheduled Mon–Fri at 16:15 ET")
    return sched
