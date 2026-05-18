"""Daily reconciliation job: match broker fills to order suggestions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ..brokers.base import BrokerAdapter
from ..config import Settings
from ..db import session_scope
from ..models import Meta
from ..services.reconciliation import persist_reconciliation, reconcile_activities

logger = logging.getLogger(__name__)

META_KEY = "reconciliation_last_run"


def run_daily_reconciliation(settings: Settings, adapter: BrokerAdapter) -> None:
    """Pull fills since last run, match to suggestions, persist results."""
    # Determine the start of the fetch window from last-run meta (with 1h overlap)
    with session_scope() as session:
        meta = session.get(Meta, META_KEY)
        if meta is not None:
            since = datetime.fromisoformat(meta.value) - timedelta(hours=1)
        else:
            since = datetime.now(UTC) - timedelta(days=7)

    # reconcile_activities (reads), persist_reconciliation (writes), and meta update
    # all run in one session — session_scope() commits on clean exit.
    # Note: the adapter also applies a 1h overlap internally, so the effective window
    # is ~2h before `since`. The unique constraint deduplicates any overlap.
    with session_scope() as session:
        results = reconcile_activities(
            session=session,
            adapter=adapter,
            since=since,
        )
        persist_reconciliation(session, results, broker=settings.broker)

        meta_row = session.get(Meta, META_KEY)
        if meta_row is None:
            session.add(Meta(key=META_KEY, value=datetime.now(UTC).isoformat()))
        else:
            meta_row.value = datetime.now(UTC).isoformat()

    logger.info(
        "run_daily_reconciliation: %d activities processed, broker=%s",
        len(results),
        settings.broker,
    )
