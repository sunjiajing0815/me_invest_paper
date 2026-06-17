"""compose_daily_report builds allocation slices (incl. cash) for the donut chart."""
from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.models import Base, BrokerAccount, PositionsSnapshot
from investor.services.charts import CASH_COLOR
from investor.services.daily_report import compose_daily_report


@pytest.fixture()
def s() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _account(session: Session, cash: float, equity: float) -> None:
    session.add(BrokerAccount(
        account_ref=61, broker="alpaca", mode="paper", nickname="Alpaca",
        is_active=True, cash_usd=cash, equity_usd=equity,
        last_sync=datetime.now(UTC), effective_to=None,
    ))


def _pos(session: Session, ticker: str, mv: float, ts: datetime) -> None:
    session.add(PositionsSnapshot(
        broker_account_id=61, ticker=ticker, qty=1.0, avg_cost=mv,
        market_value=mv, weight_pct=0.0, currency="USD", ts=ts,
    ))


def test_slices_include_cash_and_sum_to_100(s: Session) -> None:
    ts = datetime.now(UTC)
    _account(s, cash=10.0, equity=100.0)
    _pos(s, "VOO", 50.0, ts)
    _pos(s, "QQQ", 40.0, ts)  # positions 90 + cash 10 = 100 total
    s.flush()

    slices = compose_daily_report(s, broker_account_id=61).allocation_slices
    by = {sl.label: sl for sl in slices}
    assert by["VOO"].pct == pytest.approx(50.0)
    assert by["QQQ"].pct == pytest.approx(40.0)
    assert by["Cash"].pct == pytest.approx(10.0)
    assert by["Cash"].color == CASH_COLOR
    assert sum(sl.pct for sl in slices) == pytest.approx(100.0)
    # Cash is always the last slice.
    assert slices[-1].label == "Cash"


def test_small_slices_grouped_into_other(s: Session) -> None:
    ts = datetime.now(UTC)
    _account(s, cash=0.0, equity=100.0)
    # 10 tickers of 10 each -> top 8 individual, last 2 grouped into "Other".
    for i in range(10):
        _pos(s, f"T{i:02d}", 10.0, ts)
    s.flush()

    slices = compose_daily_report(s, broker_account_id=61).allocation_slices
    labels = [sl.label for sl in slices]
    assert "Other" in labels
    other = next(sl for sl in slices if sl.label == "Other")
    assert other.pct == pytest.approx(20.0)  # the 2 smallest folded in
    # 8 tickers + Other (+ no cash since cash=0)
    assert len([sl for sl in slices if sl.label not in ("Other", "Cash")]) == 8


def test_no_cash_slice_when_cash_zero_or_negative(s: Session) -> None:
    ts = datetime.now(UTC)
    _account(s, cash=0.0, equity=50.0)
    _pos(s, "VOO", 50.0, ts)
    s.flush()
    slices = compose_daily_report(s, broker_account_id=61).allocation_slices
    assert all(sl.label != "Cash" for sl in slices)


def test_no_positions_no_slices(s: Session) -> None:
    _account(s, cash=100.0, equity=100.0)
    s.flush()
    # Cash-only: a single cash slice is still useful, but no positions means the
    # donut would be one ring — acceptable. Assert it does not raise and cash shows.
    slices = compose_daily_report(s, broker_account_id=61).allocation_slices
    assert [sl.label for sl in slices] == ["Cash"]
