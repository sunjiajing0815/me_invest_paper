"""Tests for the order suggestion engine."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.models import Base, OrderSuggestion
from investor.services.daily_report import AccountSnapshot
from investor.services.gap import GapRow
from investor.services.levels import NearbyLevels, SRLevelRow
from investor.services.suggest import (
    OrderSuggestionRow,
    _next_friday_eod,
    _next_monday,
    generate_suggestions,
    persist_suggestions,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _account(equity: float = 10_000.0, cash: float = 500.0) -> AccountSnapshot:
    return AccountSnapshot(broker="alpaca", mode="paper", cash_usd=cash, equity_usd=equity)


def _gap(ticker: str, gap_pct: float, band_status: str, equity: float = 10_000.0) -> GapRow:
    current_pct = 40.0 - gap_pct
    target_pct = 40.0
    return GapRow(
        ticker=ticker,
        current_pct=current_pct,
        target_pct=target_pct,
        gap_pct=gap_pct,
        gap_usd=gap_pct / 100 * equity,
        band_status=band_status,  # type: ignore[arg-type]
    )


def _levels(
    ticker: str,
    current_price: float,
    support_price: float | None = None,
    resistance_price: float | None = None,
    method: str = "sma_50",
) -> NearbyLevels:
    today = date(2026, 5, 5)
    supports = (
        [SRLevelRow(ticker=ticker, type="support", price=support_price, method=method, as_of=today)]
        if support_price is not None
        else []
    )
    resistances = (
        [SRLevelRow(
            ticker=ticker, type="resistance", price=resistance_price, method=method, as_of=today
        )]
        if resistance_price is not None
        else []
    )
    return NearbyLevels(
        ticker=ticker, current_price=current_price, supports=supports, resistances=resistances
    )


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


# ---------------------------------------------------------------------------
# generate_suggestions tests
# ---------------------------------------------------------------------------

class TestGenerateSuggestions:
    def test_in_band_tickers_skipped(self) -> None:
        gap = [_gap("VOO", gap_pct=0.5, band_status="in_band")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=195.0)}
        result = generate_suggestions(gap_rows=gap, nearby_levels=nearby, account=_account())
        assert result == []

    def test_buy_suggestion_for_underweight(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=196.0)}  # 2% away
        result = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=2_000.0)
        )
        assert len(result) == 1
        s = result[0]
        assert s.ticker == "VOO"
        assert s.side == "buy"
        assert s.qty >= 1

    def test_sell_suggestion_for_overweight(self) -> None:
        gap = [_gap("VOO", gap_pct=-8.0, band_status="over")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, resistance_price=204.0)}  # 2% away
        result = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=2_000.0)
        )
        assert len(result) == 1
        assert result[0].side == "sell"

    def test_distance_guard_skips_far_levels(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        # Support is 15% away — beyond max_distance_pct=8
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=170.0)}
        result = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=5_000.0)
        )
        assert result == []

    def test_cash_floor_guard(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        # Support is close, but after purchase cash would fall below floor
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=195.0)}
        # gap_usd = 8% of 10k = 800; half = 400; at 195/share = 2 shares = $390
        # Cash = $400; cost = $390; cash after = $10; floor = $100 → skipped
        result = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby,
            account=_account(cash=400.0), cash_floor=100.0,
        )
        assert result == []

    def test_no_suggestion_when_no_nearby_levels(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0)}  # no support
        result = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=5_000.0)
        )
        assert result == []

    def test_result_is_frozen_dataclass(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=196.0)}
        result = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=2_000.0)
        )
        if result:
            with pytest.raises(dataclasses.FrozenInstanceError):
                result[0].qty = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# persist_suggestions tests
# ---------------------------------------------------------------------------

class TestPersistSuggestions:
    def _make_row(self, ticker: str = "VOO", side: str = "buy") -> OrderSuggestionRow:
        return OrderSuggestionRow(
            ticker=ticker,
            side=side,  # type: ignore[arg-type]
            qty=2.0,
            limit_price=195.0,
            reason="test suggestion",
            expires_at=_next_friday_eod(),
        )

    def test_inserts_new_rows(self, db_session: Session) -> None:
        week = _next_monday()
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week)
        db_session.commit()
        count = db_session.query(OrderSuggestion).count()
        assert count == 1

    def test_updates_pending_row_on_rerun(self, db_session: Session) -> None:
        week = _next_monday()
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week)
        db_session.commit()

        updated = OrderSuggestionRow(
            ticker="VOO", side="buy", qty=5.0, limit_price=190.0,
            reason="updated", expires_at=_next_friday_eod(),
        )
        persist_suggestions(db_session, [updated], None, week)
        db_session.commit()

        row = db_session.query(OrderSuggestion).first()
        assert row is not None
        assert row.qty == 5.0
        assert row.limit_price == 190.0
        assert db_session.query(OrderSuggestion).count() == 1

    def test_does_not_overwrite_accepted_row(self, db_session: Session) -> None:
        week = _next_monday()
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week)
        db_session.commit()

        # Mark as accepted
        row = db_session.query(OrderSuggestion).first()
        assert row is not None
        row.status = "accepted"
        db_session.commit()

        updated = OrderSuggestionRow(
            ticker="VOO", side="buy", qty=99.0, limit_price=1.0,
            reason="should not overwrite", expires_at=_next_friday_eod(),
        )
        persist_suggestions(db_session, [updated], None, week)
        db_session.commit()

        row = db_session.query(OrderSuggestion).first()
        assert row is not None
        assert row.qty == 2.0          # original — not overwritten
        assert row.status == "accepted"

    def test_no_duplicates_on_rerun(self, db_session: Session) -> None:
        week = _next_monday()
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week)
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week)
        db_session.commit()
        assert db_session.query(OrderSuggestion).count() == 1


# ---------------------------------------------------------------------------
# Date helper tests
# ---------------------------------------------------------------------------

class TestDateHelpers:
    def test_next_monday_is_monday(self) -> None:
        d = _next_monday()
        assert d.weekday() == 0  # Monday = 0

    def test_next_friday_eod_is_friday(self) -> None:
        dt = _next_friday_eod()
        assert dt.weekday() == 4  # Friday = 4
        assert dt.hour == 21
        assert dt.tzinfo is not None
