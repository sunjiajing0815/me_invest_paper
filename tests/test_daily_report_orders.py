"""compose_daily_report summarises this-week orders placed and filled (replaces Levels)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from investor.models import OrderExecution
from investor.services.daily_report import compose_daily_report


def _this_monday() -> date:
    today = datetime.now(UTC).date()
    return today - timedelta(days=today.weekday())


def _exe(
    session: Session,
    ticker: str,
    *,
    status: str,
    filled_qty: float,
    filled_price: float | None = None,
    side: str = "buy",
    dry_run: bool = False,
    account: int = 61,
    created_at: datetime | None = None,
    filled_at: datetime | None = None,
) -> OrderExecution:
    row = OrderExecution(
        broker_account_id=account, ticker=ticker, side=side, filled_qty=filled_qty,
        filled_price=filled_price, broker="alpaca",
        broker_order_id=f"bo-{ticker}-{status}", dry_run=dry_run, status=status,
        match_method="auto_trade_placed", filled_at=filled_at,
    )
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    session.flush()
    return row


def test_summary_counts_and_notional(s: Session) -> None:
    now = datetime.now(UTC)
    _exe(s, "VOO", status="filled", filled_qty=5.0, filled_price=400.0, filled_at=now)
    _exe(s, "QQQ", status="filled", filled_qty=2.0, filled_price=650.0, filled_at=now)
    _exe(s, "MSFT", status="accepted_for_routing", filled_qty=0.0)  # placed, not filled
    o = compose_daily_report(s, broker_account_id=61).orders_this_week
    assert o.placed_count == 3
    assert o.filled_count == 2
    assert o.filled_notional_usd == pytest.approx(5 * 400 + 2 * 650)
    assert {f.ticker for f in o.fills} == {"VOO", "QQQ"}


def test_dry_run_excluded(s: Session) -> None:
    _exe(s, "VOO", status="filled", filled_qty=5.0, filled_price=400.0, dry_run=True)
    o = compose_daily_report(s, broker_account_id=61).orders_this_week
    assert o.placed_count == 0
    assert o.fills == []


def test_other_account_excluded(s: Session) -> None:
    _exe(s, "VOO", status="filled", filled_qty=5.0, filled_price=400.0, account=62)
    o = compose_daily_report(s, broker_account_id=61).orders_this_week
    assert o.placed_count == 0


def test_last_week_excluded(s: Session) -> None:
    last_week = datetime.combine(_this_monday(), datetime.min.time(), tzinfo=UTC) - timedelta(
        days=1
    )
    _exe(s, "VOO", status="filled", filled_qty=5.0, filled_price=400.0, created_at=last_week)
    o = compose_daily_report(s, broker_account_id=61).orders_this_week
    assert o.placed_count == 0


def test_empty_week_is_zero(s: Session) -> None:
    o = compose_daily_report(s, broker_account_id=61).orders_this_week
    assert o.placed_count == 0 and o.filled_count == 0
    assert o.filled_notional_usd == 0.0 and o.fills == []
