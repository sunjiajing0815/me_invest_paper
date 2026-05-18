"""APScheduler bootstrap — Phase 2/3c/4.

Jobs:
  daily_report          — Mon–Fri 16:15 ET, grace 30 min
  suggestion_expiry     — Mon–Fri 16:20 ET, grace 30 min
  movers                — Mon–Fri 16:30 ET, grace 1 h
  daily_reconciliation  — Mon–Fri 16:45 ET, grace 30 min
  moomoo_parallel       — Mon–Fri 16:50 ET, grace 30 min
  weekly_suggestions    — Sun 18:00 ET, grace 6 h
  weekly_review         — Fri 17:00 ET, grace 1 h
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
    suggestion_expiry_func: Callable[[], None] | None = None,
    reconciliation_func: Callable[[], None] | None = None,
    moomoo_parallel_func: Callable[[], None] | None = None,
    weekly_review_func: Callable[[], None] | None = None,
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

    if suggestion_expiry_func is not None:
        sched.add_job(
            suggestion_expiry_func,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=16,
                minute=20,
                timezone="America/New_York",
            ),
            id="suggestion_expiry",
            replace_existing=True,
            misfire_grace_time=60 * 30,
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

    if reconciliation_func is not None:
        sched.add_job(
            reconciliation_func,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=16,
                minute=45,
                timezone="America/New_York",
            ),
            id="daily_reconciliation",
            replace_existing=True,
            misfire_grace_time=60 * 30,
        )

    if moomoo_parallel_func is not None:
        sched.add_job(
            moomoo_parallel_func,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour=16,
                minute=50,
                timezone="America/New_York",
            ),
            id="moomoo_parallel",
            replace_existing=True,
            misfire_grace_time=60 * 30,
        )

    if weekly_review_func is not None:
        sched.add_job(
            weekly_review_func,
            trigger=CronTrigger(
                day_of_week="fri",
                hour=17,
                minute=0,
                timezone="America/New_York",
            ),
            id="weekly_review",
            replace_existing=True,
            misfire_grace_time=60 * 60,  # 1h grace — it's a long-running job
        )

    logger.info(
        "APScheduler created. Daily report Mon–Fri 16:15 ET; Expiry sweep 16:20 ET;"
        " Movers Mon–Fri 16:30 ET; Reconciliation Mon–Fri 16:45 ET;"
        " Moomoo parallel Mon–Fri 16:50 ET; Weekly suggestions Sun 18:00 ET;"
        " Weekly review Fri 17:00 ET"
    )
    return sched
