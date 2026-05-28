"""Hash-based idempotent target allocation loader."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import TargetsConfig
from ..models import Meta, OrderSuggestion, TargetAllocation

logger = logging.getLogger(__name__)


def yaml_hash(targets_path: str) -> str:
    return hashlib.sha256(Path(targets_path).read_bytes()).hexdigest()


def load_targets_into_db(session: Session, targets: TargetsConfig, content_hash: str) -> str:
    """Idempotent. Skips writes if hash unchanged. Returns 'unchanged' or 'updated'."""
    stored = session.get(Meta, "targets_yaml_hash")
    if stored and stored.value == content_hash:
        return "unchanged"

    now = datetime.now(UTC)
    for row in (
        session.query(TargetAllocation)
        .filter(TargetAllocation.effective_to.is_(None))
        .all()
    ):
        row.effective_to = now
    session.flush()

    session.add_all([
        TargetAllocation(
            ticker=t.ticker,
            target_pct=t.pct,
            band_low_pct=t.band_low,
            band_high_pct=t.band_high,
            effective_from=now,
        )
        for t in targets.targets
    ])

    if stored:
        stored.value = content_hash
    else:
        session.add(Meta(key="targets_yaml_hash", value=content_hash))

    today = datetime.now(UTC).date()
    current_week_monday = today - timedelta(days=today.weekday())
    new_tickers = {t.ticker for t in targets.targets}

    accepted = session.scalars(
        select(OrderSuggestion).where(
            OrderSuggestion.status == "accepted",
            OrderSuggestion.week_of == current_week_monday,
        )
    ).all()
    for sug in accepted:
        if sug.ticker not in new_tickers:
            sug.status = "expired"
            sug.acted_at = now
            logger.warning(
                "load_targets: expired accepted sug-%d (%s) — ticker removed from targets",
                sug.id,
                sug.ticker,
            )
        else:
            logger.warning(
                "load_targets: sug-%d (%s) may be stale — targets changed mid-week",
                sug.id,
                sug.ticker,
            )

    return "updated"
