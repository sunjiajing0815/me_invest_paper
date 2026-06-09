"""Tests for services/unaccept.py — un-accept an accepted suggestion."""
from __future__ import annotations

from collections.abc import Generator
from datetime import date
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.models import Base, OrderExecution, OrderSuggestion
from investor.services.unaccept import UnacceptResult, unaccept_suggestion


@pytest.fixture()
def s() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _add_suggestion(session: Session, status: str = "accepted") -> OrderSuggestion:
    sug = OrderSuggestion(
        broker_account_id=61, week_of=date(2026, 6, 8), ticker="VOO", side="buy",
        qty=5.0, limit_price=400.0, reason="t", status=status,
    )
    session.add(sug)
    session.flush()
    return sug


def _add_exec(session: Session, sid: int, status: str = "accepted_for_routing") -> OrderExecution:
    e = OrderExecution(
        broker_account_id=61, suggestion_id=sid, ticker="VOO", side="buy", filled_qty=0.0,
        broker="alpaca", broker_order_id="bo-1", client_order_id=f"sug-{sid}",
        dry_run=False, status=status, match_method="auto_trade_placed",
    )
    session.add(e)
    session.flush()
    return e


def test_unaccept_working_order_cancels_and_marks_cancelled(s: Session) -> None:
    adapter = MagicMock()
    adapter.get_order.return_value = MagicMock(status="new", broker_order_id="bo-1")
    sug = _add_suggestion(s)
    _add_exec(s, sug.id)
    res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=61)
    assert res is UnacceptResult.CANCELLED
    assert s.get(OrderSuggestion, sug.id).status == "cancelled"
    adapter.cancel_order.assert_called_once_with("bo-1")


def test_unaccept_unplaced_suggestion_just_marks_cancelled(s: Session) -> None:
    adapter = MagicMock()
    sug = _add_suggestion(s)  # no execution
    res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=61)
    assert res is UnacceptResult.CANCELLED
    assert s.get(OrderSuggestion, sug.id).status == "cancelled"
    adapter.get_order.assert_not_called()


def test_unaccept_filled_refuses_and_leaves_status(s: Session) -> None:
    adapter = MagicMock()
    adapter.get_order.return_value = MagicMock(status="filled", broker_order_id="bo-1")
    sug = _add_suggestion(s)
    _add_exec(s, sug.id)
    res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=61)
    assert res is UnacceptResult.FILLED
    assert s.get(OrderSuggestion, sug.id).status == "accepted"  # unchanged
    adapter.cancel_order.assert_not_called()


def test_unaccept_partial_marks_cancelled(s: Session) -> None:
    adapter = MagicMock()
    adapter.get_order.return_value = MagicMock(status="partially_filled", broker_order_id="bo-1")
    sug = _add_suggestion(s)
    _add_exec(s, sug.id)
    res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=61)
    assert res is UnacceptResult.PARTIAL
    assert s.get(OrderSuggestion, sug.id).status == "cancelled"
    adapter.cancel_order.assert_called_once_with("bo-1")


def test_unaccept_non_accepted_is_not_actionable(s: Session) -> None:
    adapter = MagicMock()
    sug = _add_suggestion(s, status="pending")
    res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=61)
    assert res is UnacceptResult.NOT_ACTIONABLE
    assert s.get(OrderSuggestion, sug.id).status == "pending"


def test_unaccept_wrong_account_is_not_found(s: Session) -> None:
    adapter = MagicMock()
    sug = _add_suggestion(s)
    res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=62)  # different account
    assert res is UnacceptResult.NOT_FOUND


def test_unaccept_missing_suggestion(s: Session) -> None:
    adapter = MagicMock()
    res = unaccept_suggestion(s, adapter, 99999, broker_account_id=61)
    assert res is UnacceptResult.NOT_FOUND
