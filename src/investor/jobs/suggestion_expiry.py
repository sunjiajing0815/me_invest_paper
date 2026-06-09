"""Suggestion expiry sweep — marks stale suggestions as expired and cancels open GTC orders."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..brokers.base import BrokerAdapter
from ..db import session_scope
from ..models import OrderExecution, OrderSuggestion
from ..services.orders import CancelOutcome, cancel_working_execution

log = logging.getLogger(__name__)


_SessionFactory = Callable[[], AbstractContextManager[Session]]


def sweep_expired_suggestions(
    adapter: BrokerAdapter | None = None,
    session_factory: _SessionFactory | None = None,
    broker_account_id: int | None = None,
) -> None:
    """Mark stale suggestions expired; cancel any open GTC broker orders.

    Handles two cases:
    - ``pending`` suggestions past ``expires_at``: mark expired (never placed).
    - ``accepted`` suggestions past ``expires_at``: cancel the broker GTC order
      (if one exists and ``adapter`` is provided), then mark expired.

    When ``broker_account_id`` is given, only that account's suggestions are swept
    (the multi-broker path); ``None`` sweeps all accounts (back-compat / single broker).
    Runs daily at 09:00 ET — before the auto-trade pass (09:35).
    """
    factory = session_factory or session_scope
    with factory() as s:
        now = datetime.now(UTC)

        scope = (
            [OrderSuggestion.broker_account_id == broker_account_id]
            if broker_account_id is not None
            else []
        )

        # --- accepted with open broker orders: cancel first ---
        accepted_stale = s.scalars(
            select(OrderSuggestion).where(
                OrderSuggestion.status == "accepted",
                OrderSuggestion.expires_at < now,
                *scope,
            )
        ).all()

        cancelled = 0
        for sug in accepted_stale:
            if adapter is not None:
                exec_row = s.scalars(
                    select(OrderExecution).where(
                        OrderExecution.suggestion_id == sug.id,
                        OrderExecution.dry_run.is_(False),
                        OrderExecution.broker_order_id.is_not(None),
                    )
                ).first()
                if exec_row is not None:
                    outcome = cancel_working_execution(adapter, exec_row)
                    if outcome in (CancelOutcome.CANCELLED, CancelOutcome.PARTIAL):
                        cancelled += 1
                        log.info(
                            "sweep_expired_suggestions: cancelled broker order %s for sug-%d "
                            "(%s, %s)", exec_row.broker_order_id, sug.id, sug.ticker, outcome.value,
                        )
                    elif outcome is CancelOutcome.ALREADY_FILLED:
                        log.warning(
                            "sweep: sug-%d order %s filled before cancel — leaving for "
                            "reconciliation", sug.id, exec_row.broker_order_id,
                        )
            sug.status = "expired"
            sug.acted_at = now

        # --- pending (never placed): just expire ---
        pending_stale = s.scalars(
            select(OrderSuggestion).where(
                OrderSuggestion.status == "pending",
                OrderSuggestion.expires_at < now,
                *scope,
            )
        ).all()
        for sug in pending_stale:
            sug.status = "expired"
            sug.acted_at = now

        s.commit()
        log.info(
            "sweep_expired_suggestions: expired %d accepted (cancelled %d order(s)) "
            "+ %d pending suggestion(s)",
            len(accepted_stale),
            cancelled,
            len(pending_stale),
        )


def sweep_expired_suggestions_all_brokers(
    adapters: dict[int, BrokerAdapter],
    session_factory: _SessionFactory | None = None,
) -> None:
    """Run the expiry sweep for every active broker account, each via its own adapter."""
    from ..services.accounts import list_active_accounts

    factory = session_factory or session_scope
    with factory() as s:
        accounts = list_active_accounts(s)
    for acct in accounts:
        try:
            sweep_expired_suggestions(
                adapter=adapters.get(acct.account_ref),
                session_factory=session_factory,
                broker_account_id=acct.account_ref,
            )
        except Exception:
            log.exception(
                "sweep failed for account_ref=%s (%s); continuing",
                acct.account_ref, acct.nickname,
            )
            continue
