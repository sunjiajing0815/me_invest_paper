"""compose_daily_report gathers this-week accepted suggestions + their execution status."""
from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.models import Base, OrderExecution, OrderSuggestion
from investor.services.daily_report import compose_daily_report


@pytest.fixture()
def s() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _this_monday() -> date:
    today = datetime.now(UTC).date()
    return today - timedelta(days=today.weekday())


def _sug(session: Session, ticker: str, status: str = "accepted") -> OrderSuggestion:
    row = OrderSuggestion(
        broker_account_id=61, week_of=_this_monday(), ticker=ticker, side="buy",
        qty=5.0, limit_price=400.0, reason="t", status=status,
    )
    session.add(row)
    session.flush()
    return row


def test_committed_rows_classify_status(s: Session) -> None:
    a = _sug(s, "VOO")   # will have a working order
    _sug(s, "QQQ")       # accepted, no execution -> awaiting placement
    s.add(OrderExecution(
        broker_account_id=61, suggestion_id=a.id, ticker="VOO", side="buy", filled_qty=0.0,
        broker="alpaca", broker_order_id="bo-1", dry_run=False,
        status="accepted_for_routing", match_method="auto_trade_placed",
    ))
    s.flush()

    report = compose_daily_report(s, broker_account_id=61)
    by = {r.ticker: r for r in report.committed_orders}
    assert by["VOO"].status_label == "Working" and by["VOO"].cancellable is True
    assert by["QQQ"].status_label == "Awaiting placement" and by["QQQ"].cancellable is True
    assert by["VOO"].sid == a.id


def test_filled_row_not_cancellable(s: Session) -> None:
    a = _sug(s, "MU")
    s.add(OrderExecution(
        broker_account_id=61, suggestion_id=a.id, ticker="MU", side="buy", filled_qty=5.0,
        filled_price=751.0, broker="alpaca", broker_order_id="bo-2", dry_run=False,
        status="filled", match_method="auto_matched",
    ))
    s.flush()
    report = compose_daily_report(s, broker_account_id=61)
    row = next(r for r in report.committed_orders if r.ticker == "MU")
    assert row.status_label == "Filled" and row.cancellable is False
    assert row.filled_price == 751.0


def test_non_accepted_not_listed(s: Session) -> None:
    _sug(s, "AMZN", status="rejected")
    report = compose_daily_report(s, broker_account_id=61)
    assert all(r.ticker != "AMZN" for r in report.committed_orders)
