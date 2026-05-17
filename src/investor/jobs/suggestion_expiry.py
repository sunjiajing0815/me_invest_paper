"""Suggestion expiry sweep — marks stale pending suggestions as expired."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from ..db import session_scope
from ..models import OrderSuggestion

log = logging.getLogger(__name__)


def sweep_expired_suggestions(session_factory=None) -> None:
    """Mark pending OrderSuggestion rows whose expires_at is in the past as expired.

    Runs daily at 16:20 ET — between the daily report (16:15) and movers (16:30).
    Keeps the daily email's pending-count accurate.
    """
    factory = session_factory or session_scope
    with factory() as s:
        now = datetime.now(UTC)
        stale = s.scalars(
            select(OrderSuggestion).where(
                OrderSuggestion.status == "pending",
                OrderSuggestion.expires_at < now,
            )
        ).all()
        for sug in stale:
            sug.status = "expired"
            sug.acted_at = now
        s.commit()
        log.info("sweep_expired_suggestions: expired %d stale suggestion(s)", len(stale))
