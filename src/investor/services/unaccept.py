"""Un-accept an accepted suggestion: cancel any working broker order and mark it cancelled.

Reused by the magic-link confirm POST and the admin endpoint. Caller commits the session.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..brokers.base import BrokerAdapter
from ..models import OrderExecution, OrderSuggestion
from .orders import CancelOutcome, cancel_working_execution

log = logging.getLogger(__name__)


class UnacceptResult(StrEnum):
    CANCELLED = "cancelled"            # suggestion -> cancelled (broker order cancelled if any)
    PARTIAL = "partial"               # remainder cancelled, partial fill kept; -> cancelled
    FILLED = "filled"                 # order fully filled; refused, suggestion unchanged
    NOT_ACTIONABLE = "not_actionable"  # suggestion is not in the 'accepted' state
    NOT_FOUND = "not_found"


def unaccept_suggestion(
    session: Session, adapter: BrokerAdapter, suggestion_id: int, *, broker_account_id: int
) -> UnacceptResult:
    """Un-accept suggestion *suggestion_id* for *broker_account_id*. Caller commits."""
    sug = session.get(OrderSuggestion, suggestion_id)
    if sug is None or sug.broker_account_id != broker_account_id:
        return UnacceptResult.NOT_FOUND
    if sug.status != "accepted":
        return UnacceptResult.NOT_ACTIONABLE

    exe = session.scalars(
        select(OrderExecution)
        .where(
            OrderExecution.suggestion_id == sug.id,
            OrderExecution.broker_account_id == broker_account_id,
            OrderExecution.dry_run.is_(False),
            OrderExecution.broker_order_id.is_not(None),
        )
        .order_by(OrderExecution.created_at.desc())
    ).first()

    result = UnacceptResult.CANCELLED
    if exe is not None:
        outcome = cancel_working_execution(adapter, exe)
        if outcome is CancelOutcome.ALREADY_FILLED:
            return UnacceptResult.FILLED  # the trade happened — leave the suggestion as-is
        if outcome is CancelOutcome.PARTIAL:
            result = UnacceptResult.PARTIAL

    sug.status = "cancelled"
    sug.acted_at = datetime.now(UTC)
    log.info("unaccept: sug-%d (%s) -> cancelled (%s)", sug.id, sug.ticker, result.value)
    return result
