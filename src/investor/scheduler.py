"""APScheduler bootstrap — Phase 2.

Jobs:
  daily_report  — Mon–Fri 16:15 ET, grace 30 min
  weekly_suggestions — Sun 18:00 ET, grace 6 h
  movers — Mon–Fri 16:30 ET, grace 1 h
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def make_scheduler(
    daily_report_func: Callable[[], None],
    weekly_suggestions_func: Callable[[], None],
    movers_func: Callable[[], None],
) -> BackgroundScheduler:
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

    sched.add_job(
        weekly_suggestions_func,
        trigger=CronTrigger(
            day_of_week="sun",
            hour=18,
            minute=0,
            timezone="America/New_York",
        ),
        id="weekly_suggestions",
        replace_existing=True,
        misfire_grace_time=60 * 60 * 6,
    )

    sched.add_job(
        movers_func,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour=16,
            minute=30,
            timezone="America/New_York",
        ),
        id="movers",
        replace_existing=True,
        misfire_grace_time=60 * 60,
    )

    logger.info(
        "APScheduler created. Daily report Mon–Fri 16:15 ET; Weekly suggestions Sun 18:00 ET;"
        " Movers Mon–Fri 16:30 ET"
    )
    return sched
