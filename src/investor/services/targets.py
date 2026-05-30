"""Hash-based idempotent target allocation loader."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..brokers.base import BrokerAdapter
from ..config import Settings, TargetsConfig
from ..models import Meta, OrderExecution, OrderSuggestion, TargetAllocation

logger = logging.getLogger(__name__)


def yaml_hash(targets_path: str) -> str:
    return hashlib.sha256(Path(targets_path).read_bytes()).hexdigest()


def targets_path_for_account(
    settings: Settings, broker_account_id: int, *, is_primary: bool
) -> str | None:
    """Resolve the targets YAML path for one broker account.

    Prefers a per-account file at ``<data>/targets/<broker_account_id>.yaml``. The
    primary account falls back to ``settings.targets_path`` (the legacy
    ``config/targets.yaml``) for one release — non-primary accounts without a
    per-account file return ``None`` (skip; nothing to load).
    """
    per_account = Path(settings.bars_dir).parent / "targets" / f"{broker_account_id}.yaml"
    if per_account.exists():
        return str(per_account)
    if is_primary:
        return settings.targets_path
    return None


def load_targets_into_db(
    session: Session,
    targets: TargetsConfig,
    content_hash: str,
    *,
    broker_account_id: int,
    adapter: BrokerAdapter | None = None,
) -> str:
    """Idempotent per-broker target loader. Skips writes if this account's hash is
    unchanged. Returns 'unchanged' or 'updated'.

    All reads/writes (the content-hash meta key, target_allocation close-and-insert,
    and the mid-week accepted-suggestion expiry) are scoped to broker_account_id.
    """
    hash_key = f"targets_yaml_hash:{broker_account_id}"
    stored = session.get(Meta, hash_key)
    if stored and stored.value == content_hash:
        return "unchanged"

    now = datetime.now(UTC)
    for row in (
        session.query(TargetAllocation)
        .filter(
            TargetAllocation.effective_to.is_(None),
            TargetAllocation.broker_account_id == broker_account_id,
        )
        .all()
    ):
        row.effective_to = now
    session.flush()

    session.add_all([
        TargetAllocation(
            broker_account_id=broker_account_id,
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
        session.add(Meta(key=hash_key, value=content_hash))

    today = datetime.now(UTC).date()
    current_week_monday = today - timedelta(days=today.weekday())
    new_tickers = {t.ticker for t in targets.targets}

    accepted = session.scalars(
        select(OrderSuggestion).where(
            OrderSuggestion.status == "accepted",
            OrderSuggestion.week_of == current_week_monday,
            OrderSuggestion.broker_account_id == broker_account_id,
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
            if adapter is not None:
                exec_rows = session.scalars(
                    select(OrderExecution).where(
                        OrderExecution.suggestion_id == sug.id,
                        OrderExecution.broker_account_id == broker_account_id,
                        OrderExecution.status == "accepted_for_routing",
                        OrderExecution.dry_run.is_(False),
                        OrderExecution.broker_order_id.is_not(None),
                    )
                ).all()
                for exe in exec_rows:
                    try:
                        adapter.cancel_order(exe.broker_order_id)  # type: ignore[arg-type]
                        exe.status = "broker_cancelled"
                        logger.warning(
                            "load_targets: cancelled GTC order %s for expired sug-%d (%s)",
                            exe.broker_order_id,
                            sug.id,
                            sug.ticker,
                        )
                    except Exception as exc:
                        logger.warning(
                            "load_targets: cancel_order failed for sug-%d: %s"
                            " — expiry sweep will retry at 09:00 ET",
                            sug.id,
                            exc,
                        )
        else:
            logger.warning(
                "load_targets: sug-%d (%s) sizing may be stale"
                " — qty was computed against the old target pct",
                sug.id,
                sug.ticker,
            )

    return "updated"
