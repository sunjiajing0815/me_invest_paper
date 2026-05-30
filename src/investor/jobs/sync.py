"""APScheduler job wrapper for position sync.

Orchestration layer: opens a session, calls services, handles errors.
Services themselves are pure functions.
"""

from __future__ import annotations

import logging

from ..brokers import make_adapter
from ..config import Settings
from ..db import session_scope
from ..services.accounts import resolve_primary_account_ref
from ..services.snapshot import take_snapshot

logger = logging.getLogger(__name__)


def run_sync_job(settings: Settings) -> None:
    """Fetch positions from the primary broker account and persist to DB.

    Safe to call from the scheduler. Single-broker (primary) sync; the per-broker
    daily-report loop is the multi-broker path.
    """
    logger.info("run_sync_job started (broker=%s)", settings.broker)
    try:
        adapter = make_adapter(settings)
        with session_scope() as session:
            broker_account_id = resolve_primary_account_ref(session)
            if broker_account_id is None:
                logger.warning("run_sync_job: no active broker account — skipping")
                return
            n = take_snapshot(adapter, session, settings, broker_account_id)
        logger.info("run_sync_job completed: %d position rows written", n)
    except Exception:
        logger.exception("run_sync_job failed")
        raise
