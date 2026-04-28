"""Tests for compute_gap() using in-memory SQLite with seeded fake data."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.models import Base, BrokerAccount, PositionsSnapshot, TargetAllocation
from investor.services.gap import GapRow, compute_gap


@pytest.fixture()
def db_session() -> Session:
    """Provide a transactional in-memory SQLite session."""
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _seed_account(session: Session, equity: float = 10_000.0) -> datetime:
    ts = datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC)
    session.add(
        BrokerAccount(
            broker="alpaca", mode="paper",
            cash_usd=equity * 0.05, equity_usd=equity, last_sync=ts,
        )
    )
    return ts


def _seed_targets(session: Session, ts: datetime) -> None:
    session.add_all([
        TargetAllocation(
            ticker="VOO", target_pct=40.0, band_low_pct=35.0,
            band_high_pct=45.0, effective_from=ts,
        ),
        TargetAllocation(
            ticker="QQQ", target_pct=25.0, band_low_pct=21.0,
            band_high_pct=29.0, effective_from=ts,
        ),
    ])


class TestComputeGap:
    def test_returns_empty_list_with_no_data(self, db_session: Session) -> None:
        rows = compute_gap(db_session)
        assert rows == []

    def test_fully_unallocated_shows_full_gap(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        db_session.commit()

        rows = compute_gap(db_session)
        by_ticker = {r.ticker: r for r in rows}

        assert by_ticker["VOO"].current_pct == pytest.approx(0.0)
        assert by_ticker["VOO"].gap_pct == pytest.approx(40.0)
        assert by_ticker["VOO"].gap_usd == pytest.approx(4_000.0)

    def test_partial_allocation_gap_math(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        db_session.add(
            PositionsSnapshot(
                ts=ts, ticker="VOO", qty=10.0,
                avg_cost=450.0, market_value=4_600.0, weight_pct=38.5,
            )
        )
        db_session.commit()

        rows = compute_gap(db_session)
        voo = {r.ticker: r for r in rows}["VOO"]
        assert voo.current_pct == pytest.approx(38.5)
        assert voo.gap_pct == pytest.approx(1.5, abs=0.01)
        assert voo.gap_usd == pytest.approx(150.0, abs=0.01)

    def test_on_target_shows_zero_gap(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        db_session.add(
            PositionsSnapshot(
                ts=ts, ticker="VOO", qty=10.0,
                avg_cost=400.0, market_value=4_000.0, weight_pct=40.0,
            )
        )
        db_session.commit()

        voo = {r.ticker: r for r in compute_gap(db_session)}["VOO"]
        assert voo.gap_pct == pytest.approx(0.0, abs=0.01)
        assert voo.gap_usd == pytest.approx(0.0, abs=0.01)

    def test_sorted_by_abs_gap_desc(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        db_session.add(
            PositionsSnapshot(
                ts=ts, ticker="VOO", qty=1.0,
                avg_cost=4000.0, market_value=4_000.0, weight_pct=40.0,
            )
        )
        db_session.commit()

        rows = compute_gap(db_session)
        assert rows[0].ticker == "QQQ"   # 25% gap first
        assert rows[1].ticker == "VOO"   # 0% gap last

    def test_only_latest_snapshot_per_ticker_used(self, db_session: Session) -> None:
        ts1 = datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC)
        _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts2)
        db_session.add(PositionsSnapshot(
            ts=ts1, ticker="VOO", qty=5.0,
            avg_cost=400.0, market_value=2_000.0, weight_pct=20.0,
        ))
        db_session.add(PositionsSnapshot(
            ts=ts2, ticker="VOO", qty=9.5,
            avg_cost=400.0, market_value=3_800.0, weight_pct=38.0,
        ))
        db_session.commit()

        voo = {r.ticker: r for r in compute_gap(db_session)}["VOO"]
        assert voo.current_pct == pytest.approx(38.0)

    def test_closed_target_excluded(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        db_session.add(TargetAllocation(
            ticker="VOO", target_pct=40.0, band_low_pct=35.0,
            band_high_pct=45.0, effective_from=ts, effective_to=ts,
        ))
        db_session.add(TargetAllocation(
            ticker="QQQ", target_pct=25.0, band_low_pct=21.0,
            band_high_pct=29.0, effective_from=ts,
        ))
        db_session.commit()

        tickers = {r.ticker for r in compute_gap(db_session)}
        assert "VOO" not in tickers
        assert "QQQ" in tickers

    def test_gap_row_is_dataclass(self, db_session: Session) -> None:
        ts = _seed_account(db_session)
        _seed_targets(db_session, ts)
        db_session.commit()
        rows = compute_gap(db_session)
        assert all(isinstance(r, GapRow) for r in rows)
