"""APScheduler job wrapper for position sync.

Orchestration layer: opens a session, calls services, handles errors.
Services themselves are pure functions.
"""

from __future__ import annotations

import logging

from ..brokers import make_adapter
from ..config import Settings
from ..db import session_scope
from ..services.snapshot import take_snapshot

logger = logging.getLogger(__name__)


def run_sync_job(settings: Settings) -> None:
    """Fetch positions from broker and persist to DB. Safe to call from scheduler."""
    logger.info("run_sync_job started (broker=%s)", settings.broker)
    try:
        adapter = make_adapter(settings)
        with session_scope() as session:
            n = take_snapshot(adapter, session, settings)
        logger.info("run_sync_job completed: %d position rows written", n)
    except Exception:
        logger.exception("run_sync_job failed")
        raise
