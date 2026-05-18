"""Tests for jobs/weekly_review.py — WeeklyReview dataclass and helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from investor.jobs.weekly_review import SuggestionAudit, WeeklyReview, _week_start

# ── _week_start ────────────────────────────────────────────────────────────────

def test_week_start_returns_monday_midnight_utc() -> None:
    thursday = date(2026, 5, 14)  # Thursday
    result = _week_start(thursday)
    assert result.weekday() == 0  # Monday
    assert result.tzinfo is not None
    assert result.date() == date(2026, 5, 11)  # preceding Monday
    assert result.hour == 0 and result.minute == 0 and result.second == 0


def test_week_start_on_monday_returns_same_day() -> None:
    monday = date(2026, 5, 11)
    result = _week_start(monday)
    assert result.date() == monday


# ── WeeklyReview dataclass ────────────────────────────────────────────────────

def _make_review(**overrides: Any) -> WeeklyReview:
    defaults: dict[str, Any] = dict(
        week_of=date(2026, 5, 11),
        account=None,
        realized_pnl_usd=0.0,
        suggestion_audits=[],
        gap_rows=[],
        material_news={},
        auto_trade_mode="OFF",
        promotions_this_week=[],
        kill_switches_this_week=[],
        executions_this_week=0,
        preview_suggestions=[],
        moomoo_status="unavailable",
    )
    defaults.update(overrides)
    return WeeklyReview(**defaults)


def test_weekly_review_is_frozen() -> None:
    wr = _make_review()
    with pytest.raises((AttributeError, TypeError)):
        wr.realized_pnl_usd = 99.0  # type: ignore[misc]


def test_weekly_review_fields_round_trip() -> None:
    wr = _make_review(realized_pnl_usd=123.45, auto_trade_mode="DRY_RUN")
    assert wr.realized_pnl_usd == pytest.approx(123.45)
    assert wr.auto_trade_mode == "DRY_RUN"
    assert wr.suggestion_audits == []


def test_weekly_review_empty_state_no_crash() -> None:
    wr = _make_review()
    assert wr.week_of == date(2026, 5, 11)
    assert wr.executions_this_week == 0
    assert wr.moomoo_status == "unavailable"


# ── SuggestionAudit dataclass ─────────────────────────────────────────────────

def test_suggestion_audit_is_frozen() -> None:
    audit = SuggestionAudit(
        ticker="AAPL",
        side="buy",
        qty=5.0,
        limit_price=100.0,
        status="accepted",
        acted_at=None,
        filled_price=None,
        fill_status=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        audit.status = "filled"  # type: ignore[misc]


def test_suggestion_audit_fields() -> None:
    ts = datetime(2026, 5, 12, 10, 0, tzinfo=UTC)
    audit = SuggestionAudit(
        ticker="NVDA",
        side="sell",
        qty=2.0,
        limit_price=800.0,
        status="filled",
        acted_at=ts,
        filled_price=799.5,
        fill_status="filled",
    )
    assert audit.ticker == "NVDA"
    assert audit.side == "sell"
    assert audit.filled_price == pytest.approx(799.5)
    assert audit.fill_status == "filled"
    assert audit.acted_at == ts
