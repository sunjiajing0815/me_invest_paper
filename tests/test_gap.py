"""Tests for compute_gap() using in-memory SQLite with seeded fake data."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.models import Base, BrokerAccount, PositionsSnapshot, TargetAllocation
from investor.services.gap import GapRow, compute_gap, get_untracked_positions

_ACCT = 1  # account_ref for the seeded test account


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


class TestComputeGap:
    def test_returns_empty_list_with_no_data(self, db_session: Session) -> None:
        rows = compute_gap(db_session, _ACCT)
        assert rows == []

    def test_fully_unallocated_shows_full_gap(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        db_session.commit()

        rows = compute_gap(db_session, _ACCT)
        by_ticker = {r.ticker: r for r in rows}

        assert by_ticker["VOO"].current_pct == pytest.approx(0.0)
        assert by_ticker["VOO"].gap_pct == pytest.approx(40.0)
        assert by_ticker["VOO"].gap_usd == pytest.approx(4_000.0)

    def test_partial_allocation_gap_math(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        db_session.add(
            PositionsSnapshot(broker_account_id=_ACCT, 
                ts=ts, ticker="VOO", qty=10.0,
                avg_cost=450.0, market_value=4_600.0, weight_pct=38.5,
            )
        )
        db_session.commit()

        rows = compute_gap(db_session, _ACCT)
        voo = {r.ticker: r for r in rows}["VOO"]
        assert voo.current_pct == pytest.approx(38.5)
        assert voo.gap_pct == pytest.approx(1.5, abs=0.01)
        assert voo.gap_usd == pytest.approx(150.0, abs=0.01)

    def test_on_target_shows_zero_gap(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        db_session.add(
            PositionsSnapshot(broker_account_id=_ACCT, 
                ts=ts, ticker="VOO", qty=10.0,
                avg_cost=400.0, market_value=4_000.0, weight_pct=40.0,
            )
        )
        db_session.commit()

        voo = {r.ticker: r for r in compute_gap(db_session, _ACCT)}["VOO"]
        assert voo.gap_pct == pytest.approx(0.0, abs=0.01)
        assert voo.gap_usd == pytest.approx(0.0, abs=0.01)

    def test_sorted_by_abs_gap_desc(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        db_session.add(
            PositionsSnapshot(broker_account_id=_ACCT, 
                ts=ts, ticker="VOO", qty=1.0,
                avg_cost=4000.0, market_value=4_000.0, weight_pct=40.0,
            )
        )
        db_session.commit()

        rows = compute_gap(db_session, _ACCT)
        assert rows[0].ticker == "QQQ"   # 25% gap first
        assert rows[1].ticker == "VOO"   # 0% gap last

    def test_only_latest_snapshot_per_ticker_used(self, db_session: Session) -> None:
        ts1 = datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC)
        ts2 = datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC)
        _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts2)
        db_session.add(PositionsSnapshot(broker_account_id=_ACCT, 
            ts=ts1, ticker="VOO", qty=5.0,
            avg_cost=400.0, market_value=2_000.0, weight_pct=20.0,
        ))
        db_session.add(PositionsSnapshot(broker_account_id=_ACCT, 
            ts=ts2, ticker="VOO", qty=9.5,
            avg_cost=400.0, market_value=3_800.0, weight_pct=38.0,
        ))
        db_session.commit()

        voo = {r.ticker: r for r in compute_gap(db_session, _ACCT)}["VOO"]
        assert voo.current_pct == pytest.approx(38.0)

    def test_closed_target_excluded(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        db_session.add(TargetAllocation(broker_account_id=_ACCT, 
            ticker="VOO", target_pct=40.0, band_low_pct=35.0,
            band_high_pct=45.0, effective_from=ts, effective_to=ts,
        ))
        db_session.add(TargetAllocation(broker_account_id=_ACCT, 
            ticker="QQQ", target_pct=25.0, band_low_pct=21.0,
            band_high_pct=29.0, effective_from=ts,
        ))
        db_session.commit()

        tickers = {r.ticker for r in compute_gap(db_session, _ACCT)}
        assert "VOO" not in tickers
        assert "QQQ" in tickers

    def test_gap_row_is_dataclass(self, db_session: Session) -> None:
        ts = _seed_account(db_session)
        _seed_targets(db_session, ts)
        db_session.commit()
        rows = compute_gap(db_session, _ACCT)
        assert all(isinstance(r, GapRow) for r in rows)

    def test_band_status_under(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        # VOO target=40, band=[35,45] — weight 20% is below band_low 35
        db_session.add(PositionsSnapshot(broker_account_id=_ACCT, 
            ts=ts, ticker="VOO", qty=5.0,
            avg_cost=400.0, market_value=2_000.0, weight_pct=20.0,
        ))
        db_session.commit()

        voo = {r.ticker: r for r in compute_gap(db_session, _ACCT)}["VOO"]
        assert voo.band_status == "under"

    def test_band_status_over(self, db_session: Session) -> None:
        ts = _seed_account(db_session, equity=10_000.0)
        _seed_targets(db_session, ts)
        # VOO target=40, band=[35,45] — weight 50% is above band_high 45
        db_session.add(PositionsSnapshot(broker_account_id=_ACCT, 
            ts=ts, ticker="VOO", qty=10.0,
            avg_cost=500.0, market_value=5_000.0, weight_pct=50.0,
        ))
        db_session.commit()

        voo = {r.ticker: r for r in compute_gap(db_session, _ACCT)}["VOO"]
        assert voo.band_status == "over"

    def test_cash_buffer_invariant(self, db_session: Session) -> None:
        """When every ticker is exactly at its target weight and targets sum to
        (100 - cash_buffer_pct), both gap_pct and gap_usd must be zero for all rows.

        Closes Phase 2 carryover: verifies the SQL denominator is consistent —
        weight_pct uses total equity (incl. cash) as denominator, targets sum to
        100 - cash_buffer_pct, so no scaling is required.
        """
        equity = 100_000.0
        cash = 5_000.0
        ts = datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC)

        # Targets sum to exactly 95% (= 100 - 5% cash buffer)
        tickers = [
            ("VOO",  25.0, 20.0, 30.0),
            ("QQQ",  20.0, 15.0, 25.0),
            ("AAPL", 15.0, 10.0, 20.0),
            ("MSFT", 15.0, 10.0, 20.0),
            ("NVDA", 10.0,  5.0, 15.0),
            ("BND",  10.0,  5.0, 15.0),
        ]
        assert sum(t for _, t, *_ in tickers) == 95.0  # sanity-check targets sum

        db_session.add(
            BrokerAccount(account_ref=_ACCT, 
                broker="alpaca", mode="paper",
                cash_usd=cash, equity_usd=equity, last_sync=ts,
            )
        )
        for ticker, target_pct, band_low, band_high in tickers:
            db_session.add(TargetAllocation(broker_account_id=_ACCT, 
                ticker=ticker, target_pct=target_pct,
                band_low_pct=band_low, band_high_pct=band_high,
                effective_from=ts,
            ))
            market_value = target_pct / 100.0 * equity
            db_session.add(PositionsSnapshot(broker_account_id=_ACCT, 
                ts=ts, ticker=ticker,
                qty=1.0, avg_cost=market_value, market_value=market_value,
                weight_pct=target_pct,   # pct of total equity, matches target exactly
            ))
        db_session.commit()

        rows = compute_gap(db_session, _ACCT)
        assert len(rows) == len(tickers)
        for row in rows:
            assert row.gap_pct == pytest.approx(0.0, abs=0.01), (
                f"{row.ticker}: expected gap_pct=0, got {row.gap_pct}"
            )
            assert row.gap_usd == pytest.approx(0.0, abs=0.01), (
                f"{row.ticker}: expected gap_usd=0, got {row.gap_usd}"
            )


class TestComputeGapPerBrokerIsolation:
    def test_gap_is_scoped_per_account(self, db_session: Session) -> None:
        """compute_gap(account) must return only that account's positions + targets."""
        ts = datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC)
        acct_a, acct_b = 1, 2
        # Account A: VOO target, fully unallocated
        db_session.add(BrokerAccount(
            account_ref=acct_a, broker="alpaca", mode="paper",
            cash_usd=500.0, equity_usd=10_000.0, last_sync=ts,
        ))
        db_session.add(TargetAllocation(
            broker_account_id=acct_a, ticker="VOO", target_pct=40.0,
            band_low_pct=35.0, band_high_pct=45.0, effective_from=ts,
        ))
        # Account B: TSLA target + a TSLA position
        db_session.add(BrokerAccount(
            account_ref=acct_b, broker="moomoo", mode="paper",
            cash_usd=500.0, equity_usd=20_000.0, last_sync=ts,
        ))
        db_session.add(TargetAllocation(
            broker_account_id=acct_b, ticker="TSLA", target_pct=50.0,
            band_low_pct=45.0, band_high_pct=55.0, effective_from=ts,
        ))
        db_session.add(PositionsSnapshot(
            broker_account_id=acct_b, ts=ts, ticker="TSLA", qty=10.0,
            avg_cost=1000.0, market_value=10_000.0, weight_pct=50.0,
        ))
        db_session.commit()

        a_tickers = {r.ticker for r in compute_gap(db_session, acct_a)}
        b_rows = {r.ticker: r for r in compute_gap(db_session, acct_b)}

        assert a_tickers == {"VOO"}                  # B's TSLA must not leak into A
        assert set(b_rows) == {"TSLA"}               # A's VOO must not leak into B
        assert b_rows["TSLA"].band_status == "in_band"


def test_untracked_positions_carry_native_currency(db_session: Session) -> None:
    """An untracked holding returns its stored native currency (e.g. AUD for an ASX stock)."""
    ts = _seed_account(db_session)
    db_session.add(PositionsSnapshot(
        broker_account_id=_ACCT, ts=ts, ticker="CSL", qty=25.0,
        avg_cost=130.0, market_value=2415.0, weight_pct=5.5, currency="AUD",
    ))
    db_session.commit()
    untracked = get_untracked_positions(db_session, _ACCT)
    assert len(untracked) == 1
    assert untracked[0].ticker == "CSL"
    assert untracked[0].currency == "AUD"
