"""Tests for compose_daily_report() using in-memory SQLite."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.models import Base, BrokerAccount, PositionsSnapshot, TargetAllocation
from investor.services.daily_report import DailyReport, compose_daily_report

_ACCT = 1  # account_ref for the seeded test account


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _seed_account(session: Session, equity: float = 10_000.0) -> datetime:
    ts = datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC)
    session.add(
        BrokerAccount(account_ref=_ACCT, 
            broker="alpaca", mode="paper",
            cash_usd=equity * 0.05, equity_usd=equity, last_sync=ts,
        )
    )
    return ts


def _seed_targets(session: Session, ts: datetime) -> None:
    session.add_all([
        TargetAllocation(broker_account_id=_ACCT, 
            ticker="VOO", target_pct=40.0, band_low_pct=35.0,
            band_high_pct=45.0, effective_from=ts,
        ),
        TargetAllocation(broker_account_id=_ACCT, 
            ticker="QQQ", target_pct=25.0, band_low_pct=21.0,
            band_high_pct=29.0, effective_from=ts,
        ),
    ])


def test_account_snapshot_survives_session_close(tmp_path: object) -> None:
    """Regression: AccountSnapshot fields must be readable after the session closes.

    If compose_daily_report returned an ORM BrokerAccount instead of AccountSnapshot,
    accessing its attributes here would raise sqlalchemy.orm.exc.DetachedInstanceError.
    """
    from sqlalchemy import create_engine as _ce
    engine = _ce(f"sqlite:///{tmp_path}/t.db", poolclass=StaticPool, future=True)  # type: ignore[arg-type]
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)

    ts = datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add(BrokerAccount(account_ref=_ACCT, 
            broker="alpaca", mode="paper",
            cash_usd=500.0, equity_usd=10_000.0, last_sync=ts,
        ))
        session.commit()
        report = compose_daily_report(session, broker_account_id=_ACCT)
    # Session is now closed — ORM lazy-loads would raise DetachedInstanceError here.
    assert report.account is not None
    assert report.account.equity_usd == pytest.approx(10_000.0)
    assert report.account.cash_usd == pytest.approx(500.0)
    engine.dispose()


class TestComposeDailyReport:
    def test_compose_report_empty_db(self, db_session: Session) -> None:
        report = compose_daily_report(db_session, broker_account_id=_ACCT)
        assert isinstance(report, DailyReport)
        assert report.account is None
        assert report.positions == []
        assert report.gap_rows == []
        assert report.drift_alerts == []

    def test_compose_report_drift_alerts(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        # VOO at 20% — below band_low of 35%, so should be in drift_alerts
        db_session.add(PositionsSnapshot(broker_account_id=_ACCT, 
            ts=ts, ticker="VOO", qty=5.0,
            avg_cost=400.0, market_value=2_000.0, weight_pct=20.0,
        ))
        db_session.commit()

        report = compose_daily_report(db_session, broker_account_id=_ACCT)
        assert report.account is not None
        assert report.account.equity_usd == pytest.approx(10_000.0)
        # VOO at 20% is below band_low=35 (under); QQQ at 0% is below band_low=21 (under)
        drift_tickers = {r.ticker for r in report.drift_alerts}
        assert "VOO" in drift_tickers
        voo = next(r for r in report.drift_alerts if r.ticker == "VOO")
        assert voo.band_status == "under"
