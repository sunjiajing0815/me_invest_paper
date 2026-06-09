"""Tests for services/orders.py — the shared broker-order cancel helper."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from investor.services.orders import CancelOutcome, cancel_working_execution


def _exe(status: str = "accepted_for_routing", boid: str = "bo-1") -> SimpleNamespace:
    return SimpleNamespace(broker_order_id=boid, status=status)


def _conf(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        broker_order_id="bo-1", client_order_id="sug-1",
        status=status, submitted_at=datetime.now(UTC),
    )


def test_working_order_cancelled() -> None:
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("new")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.CANCELLED
    adapter.cancel_order.assert_called_once_with("bo-1")
    assert exe.status == "broker_cancelled"


def test_filled_order_not_cancelled() -> None:
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("filled")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.ALREADY_FILLED
    adapter.cancel_order.assert_not_called()
    assert exe.status == "accepted_for_routing"  # unchanged


def test_partial_order_cancels_remainder_keeps_status() -> None:
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("partially_filled")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.PARTIAL
    adapter.cancel_order.assert_called_once_with("bo-1")
    assert exe.status == "accepted_for_routing"  # reconciliation will set the fill


def test_already_terminal_is_noop() -> None:
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("canceled")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.NOOP
    adapter.cancel_order.assert_not_called()
    assert exe.status == "broker_cancelled"


def test_get_order_raises_attempts_cancel_conservatively() -> None:
    adapter = MagicMock()
    adapter.get_order.side_effect = RuntimeError("broker down")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.CANCELLED
    adapter.cancel_order.assert_called_once_with("bo-1")
    assert exe.status == "broker_cancelled"
