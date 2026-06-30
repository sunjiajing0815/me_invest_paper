"""Tests for the cash-flow funds-detection heuristic (P2.3, ADR-0035)."""
from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.models import Base, BrokerAccount, OrderExecution
from investor.services.funds import detect_funds_flow

_NOW = datetime.now(UTC)
_YESTERDAY = _NOW - timedelta(days=2)  # safely before today-start (ET)


@pytest.fixture()
def s() -> Generator[Session, None, None]:
    eng = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(eng)
    with Session(eng) as session:
        yield session
    eng.dispose()


def _acct(session: Session, cash: float, *, last_sync: datetime, closed: bool) -> None:
    session.add(BrokerAccount(
        account_ref=1, broker="alpaca", mode="paper", nickname="A", is_active=True,
        cash_usd=cash, equity_usd=cash, last_sync=last_sync,
        effective_from=last_sync, effective_to=(_NOW if closed else None),
    ))


def _fill(session: Session, side: str, qty: float, price: float, when: datetime) -> None:
    session.add(OrderExecution(
        broker_account_id=1, ticker="VOO", side=side, filled_qty=qty, filled_price=price,
        broker="alpaca", broker_order_id=f"bo-{side}-{when.timestamp()}", dry_run=False,
        status="filled", match_method="auto_matched", filled_at=when,
    ))


def test_deposit_detected(s: Session) -> None:
    _acct(s, 1000.0, last_sync=_YESTERDAY, closed=True)
    _acct(s, 6000.0, last_sync=_NOW, closed=False)
    s.flush()
    flow = detect_funds_flow(s, 1, threshold=500.0)
    assert flow is not None and flow.kind == "deposit"
    assert flow.delta_usd == pytest.approx(5000.0)


def test_withdrawal_detected(s: Session) -> None:
    _acct(s, 6000.0, last_sync=_YESTERDAY, closed=True)
    _acct(s, 1000.0, last_sync=_NOW, closed=False)
    s.flush()
    flow = detect_funds_flow(s, 1, threshold=500.0)
    assert flow is not None and flow.kind == "withdrawal"
    assert flow.delta_usd == pytest.approx(-5000.0)


def test_cash_change_explained_by_sell_is_not_flagged(s: Session) -> None:
    _acct(s, 1000.0, last_sync=_YESTERDAY, closed=True)
    _acct(s, 6000.0, last_sync=_NOW, closed=False)
    _fill(s, "sell", 10.0, 500.0, _NOW - timedelta(hours=1))  # +5000 proceeds explains Δcash
    s.flush()
    assert detect_funds_flow(s, 1, threshold=500.0) is None


def test_sub_threshold_ignored(s: Session) -> None:
    _acct(s, 1000.0, last_sync=_YESTERDAY, closed=True)
    _acct(s, 1300.0, last_sync=_NOW, closed=False)  # +300 < 500 (dividend-ish)
    s.flush()
    assert detect_funds_flow(s, 1, threshold=500.0) is None


def test_no_prior_row_returns_none(s: Session) -> None:
    _acct(s, 1000.0, last_sync=_NOW, closed=False)  # only today's row
    s.flush()
    assert detect_funds_flow(s, 1, threshold=500.0) is None
