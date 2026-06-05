"""Daily reconciliation job: match broker fills to order suggestions (per broker account)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ..brokers.base import BrokerAdapter
from ..config import Settings
from ..db import session_scope
from ..models import Meta, OrderExecution
from ..services.accounts import (
    AccountInfo,
    list_active_accounts,
)
from ..services.reconciliation import (
    persist_reconciliation,
    reconcile_activities,
    sync_open_order_statuses,
)

logger = logging.getLogger(__name__)

_META_KEY_PREFIX = "reconciliation_last_run"


def _meta_key(account_ref: int) -> str:
    return f"{_META_KEY_PREFIX}:{account_ref}"


def run_daily_reconciliation_for_account(
    settings: Settings, adapter: BrokerAdapter, account: AccountInfo
) -> None:
    """Pull fills since last run for ONE account, match to its suggestions, persist."""
    meta_key = _meta_key(account.account_ref)

    with session_scope() as session:
        meta = session.get(Meta, meta_key)
        if meta is not None:
            since = datetime.fromisoformat(meta.value) - timedelta(hours=1)
        else:
            since = datetime.now(UTC) - timedelta(days=7)

        # Extend the window back to the oldest still-open (accepted_for_routing) execution.
        # get_activities filters by submitted_at, so a GTC order that fills days after it was
        # submitted is missed once `since` advances past its submission — leaving it stuck at
        # accepted_for_routing forever (the BTC case). Reaching back to the oldest open order
        # guarantees its eventual fill is reconciled; the window self-narrows once it clears.
        oldest_open = session.scalars(
            select(OrderExecution.created_at)
            .where(
                OrderExecution.broker_account_id == account.account_ref,
                OrderExecution.status == "accepted_for_routing",
                OrderExecution.dry_run.is_(False),
            )
            .order_by(OrderExecution.created_at.asc())
            .limit(1)
        ).first()
        if oldest_open is not None:
            since = min(since, oldest_open - timedelta(hours=1))

    with session_scope() as session:
        results = reconcile_activities(
            session=session,
            adapter=adapter,
            since=since,
            broker_account_id=account.account_ref,
        )
        persist_reconciliation(
            session, results, broker=account.broker, broker_account_id=account.account_ref
        )
        synced = sync_open_order_statuses(session, adapter, account.account_ref)

        meta_row = session.get(Meta, meta_key)
        if meta_row is None:
            session.add(Meta(key=meta_key, value=datetime.now(UTC).isoformat()))
        else:
            meta_row.value = datetime.now(UTC).isoformat()

    logger.info(
        "reconciliation: %s account_ref=%s — %d activities, %d open execs synced",
        account.nickname, account.account_ref, len(results), synced,
    )


def run_daily_reconciliation_all_brokers(
    settings: Settings, adapters: dict[int, BrokerAdapter]
) -> None:
    """Reconcile every active broker account, isolating per-account failures."""
    with session_scope() as session:
        accounts = list_active_accounts(session)
    for account in accounts:
        adapter = adapters.get(account.account_ref)
        if adapter is None:
            logger.warning(
                "reconciliation: no adapter for account_ref=%s (%s); skipping",
                account.account_ref, account.nickname,
            )
            continue
        try:
            run_daily_reconciliation_for_account(settings, adapter, account)
        except Exception:
            logger.exception(
                "reconciliation failed for account_ref=%s (%s); continuing",
                account.account_ref, account.nickname,
            )
            continue
