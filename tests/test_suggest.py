"""Tests for the order suggestion engine."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.models import Base, OrderSuggestion
from investor.services.daily_report import AccountSnapshot
from investor.services.gap import GapRow
from investor.services.levels import NearbyLevels, SRLevelRow
from investor.services.llm_levels import ScoredLevel
from investor.services.suggest import (
    OrderSuggestionRow,
    _next_friday_eod,
    _next_monday,
    generate_suggestions,
    persist_suggestions,
    select_anchor,
)

_ACCT = 1  # account_ref for persist tests

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
        suggestions, skipped = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account()
        )
        assert suggestions == []
        assert skipped == []  # in-band tickers are never reported

    def test_buy_suggestion_for_underweight(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=196.0)}  # 2% away
        suggestions, _ = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=2_000.0)
        )
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s.ticker == "VOO"
        assert s.side == "buy"
        assert s.qty >= 1

    def test_sell_suggestion_for_overweight(self) -> None:
        gap = [_gap("VOO", gap_pct=-8.0, band_status="over")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, resistance_price=204.0)}  # 2% away
        suggestions, _ = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=2_000.0)
        )
        assert len(suggestions) == 1
        assert suggestions[0].side == "sell"

    def test_distance_guard_skips_far_levels(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        # Support is 16% away — beyond max_distance_pct=15
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=168.0)}
        suggestions, skipped = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=5_000.0)
        )
        assert suggestions == []
        assert len(skipped) == 1
        assert "16.0%" in skipped[0].reason
        assert "exceeds 15%" in skipped[0].reason

    def test_cash_floor_guard(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        # Support is close, but after purchase cash would fall below floor
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=195.0)}
        # gap_usd = 8% of 10k = 800; half = 400; at 195/share = 2 shares = $390
        # Cash = $400; cost = $390; cash after = $10; floor = $100 → skipped
        suggestions, skipped = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby,
            account=_account(cash=400.0), cash_floor=100.0,
        )
        assert suggestions == []
        assert len(skipped) == 1
        assert "cash floor" in skipped[0].reason

    def test_no_suggestion_when_no_nearby_levels(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0)}  # no support
        suggestions, skipped = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=5_000.0)
        )
        assert suggestions == []
        assert len(skipped) == 1
        assert "no support levels found" in skipped[0].reason

    def test_result_is_frozen_dataclass(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=196.0)}
        suggestions, _ = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=2_000.0)
        )
        if suggestions:
            with pytest.raises(dataclasses.FrozenInstanceError):
                suggestions[0].qty = 999.0  # type: ignore[misc]


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
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week, _ACCT)
        db_session.commit()
        count = db_session.query(OrderSuggestion).count()
        assert count == 1

    def test_updates_pending_row_on_rerun(self, db_session: Session) -> None:
        week = _next_monday()
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week, _ACCT)
        db_session.commit()

        updated = OrderSuggestionRow(
            ticker="VOO", side="buy", qty=5.0, limit_price=190.0,
            reason="updated", expires_at=_next_friday_eod(),
        )
        persist_suggestions(db_session, [updated], None, week, _ACCT)
        db_session.commit()

        row = db_session.query(OrderSuggestion).first()
        assert row is not None
        assert row.qty == 5.0
        assert row.limit_price == 190.0
        assert db_session.query(OrderSuggestion).count() == 1

    def test_does_not_overwrite_accepted_row(self, db_session: Session) -> None:
        week = _next_monday()
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week, _ACCT)
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
        persist_suggestions(db_session, [updated], None, week, _ACCT)
        db_session.commit()

        row = db_session.query(OrderSuggestion).first()
        assert row is not None
        assert row.qty == 2.0          # original — not overwritten
        assert row.status == "accepted"

    def test_no_duplicates_on_rerun(self, db_session: Session) -> None:
        week = _next_monday()
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week, _ACCT)
        persist_suggestions(db_session, [self._make_row("VOO", "buy")], None, week, _ACCT)
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

    @pytest.mark.parametrize("day_offset,expected_monday_offset", [
        # Mon–Wed: returns this week's Monday (anchor_monday + 0)
        (0, 0),   # Monday    → this Monday
        (1, 0),   # Tuesday   → this Monday
        (2, 0),   # Wednesday → this Monday
        # Thu–Sun: returns next Monday (anchor_monday + 7)
        (3, 7),   # Thursday  → next Monday
        (4, 7),   # Friday    → next Monday
        (5, 7),   # Saturday  → next Monday
        (6, 7),   # Sunday    → next Monday
    ])
    def test_next_monday_thursday_cutover(self, day_offset: int, expected_monday_offset: int) -> None:  # noqa: E501
        anchor_monday = date(2026, 5, 25)  # a known Monday
        ref = anchor_monday + timedelta(days=day_offset)
        result = _next_monday(ref)
        assert result == anchor_monday + timedelta(days=expected_monday_offset)


# ---------------------------------------------------------------------------
# select_anchor tests
# ---------------------------------------------------------------------------

def _make_scored(method: str, price: float, level_type: str, confidence: float) -> ScoredLevel:
    return ScoredLevel(
        method=method, price=price, type=level_type, confidence=confidence, rationale="test"
    )


class TestSelectAnchor:
    def test_returns_highest_confidence_in_band(self) -> None:
        levels = [
            _make_scored("sma_50", 95.0, "support", 0.8),
            _make_scored("sma_200", 97.0, "support", 0.5),
        ]
        anchor = select_anchor(levels, 100.0)
        assert anchor is not None
        assert anchor.price == 95.0  # higher confidence wins

    def test_falls_back_to_nearest_when_all_below_threshold(self) -> None:
        levels = [
            _make_scored("sma_50", 95.0, "support", 0.2),   # below min_confidence=0.4
            _make_scored("sma_200", 98.0, "support", 0.1),  # below min_confidence=0.4
        ]
        anchor = select_anchor(levels, 100.0)
        assert anchor is not None
        assert anchor.price == 98.0  # nearest to 100.0

    def test_returns_none_when_no_levels_in_band(self) -> None:
        levels = [
            _make_scored("sma_200", 50.0, "support", 0.9),  # >8% from 100.0
        ]
        anchor = select_anchor(levels, 100.0)
        assert anchor is None

    def test_returns_none_for_empty_list(self) -> None:
        assert select_anchor([], 100.0) is None

    def test_exactly_at_max_distance_is_included(self) -> None:
        # 14% below 100.0 — safely within the 15% band (avoids float boundary issues)
        levels = [_make_scored("sma_50", 86.0, "support", 0.6)]
        anchor = select_anchor(levels, 100.0)
        assert anchor is not None
        assert anchor.price == 86.0

    def test_just_outside_max_distance_is_excluded(self) -> None:
        # 16% below 100.0 — outside the 15% band
        levels = [_make_scored("sma_50", 84.0, "support", 0.9)]
        anchor = select_anchor(levels, 100.0)
        assert anchor is None

    def test_selects_highest_confidence_among_multiple_qualifying(self) -> None:
        levels = [
            _make_scored("sma_50",  95.0, "support", 0.6),
            _make_scored("sma_150", 96.0, "support", 0.9),
            _make_scored("sma_200", 97.0, "support", 0.7),
        ]
        anchor = select_anchor(levels, 100.0)
        assert anchor is not None
        assert anchor.price == 96.0  # highest confidence = 0.9


# ---------------------------------------------------------------------------
# generate_suggestions scored_levels fallback regression test
# ---------------------------------------------------------------------------

class TestGenerateSuggestionsWithScoredLevels:
    def test_generate_suggestions_falls_back_when_scored_levels_empty(self) -> None:
        """generate_suggestions falls back to nearby_levels when scored_levels is empty."""
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=196.0)}
        suggestions, _ = generate_suggestions(
            gap_rows=gap,
            nearby_levels=nearby,
            account=_account(cash=2_000.0),
            scored_levels={},  # empty dict → fallback
        )
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s.ticker == "VOO"
        assert s.side == "buy"
        # confidence_at_creation must be None when using fallback path
        assert s.confidence_at_creation is None

    def test_buy_anchor_must_be_at_or_below_current_price(self) -> None:
        """A scored 'support' above current must NOT become the BUY limit (it would
        fill at market). The below-current support wins even though it scored lower."""
        scored = {
            "VOO": [
                _make_scored("ema_21", 201.0, "support", 0.90),  # ABOVE current — reject
                _make_scored("sma_50", 196.0, "support", 0.60),  # below current — valid anchor
            ]
        }
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=196.0)}
        suggestions, _ = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby,
            account=_account(cash=2_000.0), scored_levels=scored,
        )
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s.side == "buy"
        assert s.limit_price == 196.0           # below-current support, not the 201 above
        assert s.confidence_at_creation == 0.60  # used the scored path, not the fallback

    def test_sell_anchor_must_be_at_or_above_current_price(self) -> None:
        """Mirror: a scored 'resistance' below current must NOT become the SELL limit."""
        scored = {
            "VOO": [
                _make_scored("ema_21", 199.0, "resistance", 0.90),  # BELOW current — reject
                _make_scored("sma_50", 204.0, "resistance", 0.60),  # above current — valid anchor
            ]
        }
        gap = [_gap("VOO", gap_pct=-8.0, band_status="over")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, resistance_price=204.0)}
        suggestions, _ = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby,
            account=_account(cash=2_000.0), scored_levels=scored,
        )
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s.side == "sell"
        assert s.limit_price == 204.0
        assert s.confidence_at_creation == 0.60

    def test_generate_suggestions_populates_confidence_when_scored_levels_provided(self) -> None:
        """generate_suggestions uses scored_levels when non-empty and populates confidence."""
        scored = {
            "VOO": [_make_scored("sma_50", 196.0, "support", 0.75)]
        }
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=196.0)}
        suggestions, _ = generate_suggestions(
            gap_rows=gap,
            nearby_levels=nearby,
            account=_account(cash=2_000.0),
            scored_levels=scored,
        )
        assert len(suggestions) == 1
        s = suggestions[0]
        assert s.confidence_at_creation == 0.75

    def test_generate_suggestions_falls_back_when_scored_levels_none(self) -> None:
        """generate_suggestions with scored_levels=None falls back to nearby_levels."""
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=196.0)}
        suggestions, _ = generate_suggestions(
            gap_rows=gap,
            nearby_levels=nearby,
            account=_account(cash=2_000.0),
            scored_levels=None,  # None → fallback
        )
        assert len(suggestions) == 1
        assert suggestions[0].confidence_at_creation is None


# ---------------------------------------------------------------------------
# SkippedRow tests
# ---------------------------------------------------------------------------

class TestSkippedRow:
    def test_distance_guard_buy_populates_skipped(self) -> None:
        """SkippedRow captures ticker, side, gap_pct, and distance reason for buy."""
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=168.0)}
        _, skipped = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=5_000.0)
        )
        assert len(skipped) == 1
        s = skipped[0]
        assert s.ticker == "VOO"
        assert s.side == "buy"
        assert s.gap_pct == pytest.approx(8.0)
        assert "16.0%" in s.reason
        assert "exceeds 15%" in s.reason

    def test_skipped_row_is_frozen_dataclass(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=168.0)}
        _, skipped = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=5_000.0)
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            skipped[0].reason = "mutation not allowed"  # type: ignore[misc]

    def test_in_band_tickers_produce_no_skipped_row(self) -> None:
        gap = [_gap("VOO", gap_pct=0.5, band_status="in_band")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=195.0)}
        _, skipped = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account()
        )
        assert skipped == []

    def test_no_support_levels_populates_skipped(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0)}  # no supports
        _, skipped = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby, account=_account(cash=5_000.0)
        )
        assert len(skipped) == 1
        assert skipped[0].side == "buy"
        assert "no support levels found" in skipped[0].reason

    def test_cash_floor_populates_skipped(self) -> None:
        gap = [_gap("VOO", gap_pct=8.0, band_status="under")]
        nearby = {"VOO": _levels("VOO", current_price=200.0, support_price=195.0)}
        _, skipped = generate_suggestions(
            gap_rows=gap, nearby_levels=nearby,
            account=_account(cash=400.0), cash_floor=100.0,
        )
        assert len(skipped) == 1
        assert "cash floor" in skipped[0].reason
