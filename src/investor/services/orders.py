"""Broker-order cancellation helper shared by the expiry sweep and un-accept.

Re-queries the broker before cancelling so a just-filled order is never clobbered.
``OrderConfirmation`` has no ``filled_qty``, so a partial fill is detected by status only —
the filled quantity is recorded later by reconciliation (``get_activities``).
"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from ..brokers.base import BrokerAdapter

log = logging.getLogger(__name__)

_TERMINAL = frozenset({"canceled", "expired", "rejected", "done_for_day", "replaced"})


class CancelOutcome(StrEnum):
    CANCELLED = "cancelled"            # was working; cancel sent; execution -> broker_cancelled
    PARTIAL = "partial"               # partially filled; remainder cancelled; fill stands
    ALREADY_FILLED = "already_filled"  # fully filled; nothing cancelled
    NOOP = "noop"                     # already terminal at the broker


def cancel_working_execution(adapter: BrokerAdapter, execution: Any) -> CancelOutcome:
    """Cancel a working broker order for *execution*; mutate ``execution.status`` in place.

    *execution* must expose ``.broker_order_id`` and ``.status``. Caller commits the session.
    """
    boid = execution.broker_order_id
    try:
        conf = adapter.get_order(boid)
    except Exception as exc:  # noqa: BLE001 — broker unreachable: cancel conservatively
        log.warning("cancel_working_execution: get_order(%s) failed: %s", boid, exc)
        _try_cancel(adapter, boid)
        execution.status = "broker_cancelled"
        return CancelOutcome.CANCELLED

    status = conf.status
    if status == "filled":
        return CancelOutcome.ALREADY_FILLED
    if status in _TERMINAL:
        execution.status = "broker_cancelled"
        return CancelOutcome.NOOP

    _try_cancel(adapter, boid)
    if status == "partially_filled":
        return CancelOutcome.PARTIAL  # leave status; reconciliation records the fill
    execution.status = "broker_cancelled"
    return CancelOutcome.CANCELLED


def _try_cancel(adapter: BrokerAdapter, broker_order_id: str) -> None:
    try:
        adapter.cancel_order(broker_order_id)
    except Exception as exc:  # noqa: BLE001 — already-terminal cancels can 422; log + proceed
        log.warning(
            "cancel_working_execution: cancel_order(%s) failed: %s", broker_order_id, exc
        )
