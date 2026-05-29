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

log = logging.getLogger(__name__)


_SessionFactory = Callable[[], AbstractContextManager[Session]]


def sweep_expired_suggestions(
    adapter: BrokerAdapter | None = None,
    session_factory: _SessionFactory | None = None,
) -> None:
    """Mark stale suggestions expired; cancel any open GTC broker orders.

    Handles two cases:
    - ``pending`` suggestions past ``expires_at``: mark expired (never placed).
    - ``accepted`` suggestions past ``expires_at``: cancel the broker GTC order
      (if one exists and ``adapter`` is provided), then mark expired.

    Runs daily at 16:20 ET — between the daily report (16:15) and movers (16:30).
    """
    factory = session_factory or session_scope
    with factory() as s:
        now = datetime.now(UTC)

        # --- accepted with open broker orders: cancel first ---
        accepted_stale = s.scalars(
            select(OrderSuggestion).where(
                OrderSuggestion.status == "accepted",
                OrderSuggestion.expires_at < now,
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
                    try:
                        adapter.cancel_order(exec_row.broker_order_id)  # type: ignore[arg-type]
                        # Verify the broker confirmed cancellation — it may have just filled
                        try:
                            conf = adapter.get_order(exec_row.broker_order_id)  # type: ignore[arg-type]
                            if conf.status == "filled":
                                log.warning(
                                    "sweep: sug-%d order %s filled before cancel"
                                    " — leaving for reconciliation",
                                    sug.id,
                                    exec_row.broker_order_id,
                                )
                            else:
                                exec_row.status = "broker_cancelled"
                                cancelled += 1
                                log.info(
                                    "sweep_expired_suggestions: cancelled broker"
                                    " order %s for sug-%d (%s)",
                                    exec_row.broker_order_id,
                                    sug.id,
                                    sug.ticker,
                                )
                        except Exception as verify_exc:
                            # Can't verify: assume cancel succeeded (conservative)
                            exec_row.status = "broker_cancelled"
                            cancelled += 1
                            log.warning(
                                "sweep: get_order verification failed after cancel for sug-%d: %s",
                                sug.id,
                                verify_exc,
                            )
                    except Exception as exc:
                        log.warning(
                            "sweep_expired_suggestions: cancel_order failed for sug-%d "
                            "broker_order_id=%s: %s",
                            sug.id,
                            exec_row.broker_order_id,
                            exc,
                        )
            sug.status = "expired"
            sug.acted_at = now

        # --- pending (never placed): just expire ---
        pending_stale = s.scalars(
            select(OrderSuggestion).where(
                OrderSuggestion.status == "pending",
                OrderSuggestion.expires_at < now,
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
