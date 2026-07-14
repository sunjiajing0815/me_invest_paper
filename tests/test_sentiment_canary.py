"""Tests for the CNN sentiment canary (P1.5)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from investor.models import WeeklyMarketContextRow
from investor.services.weekly_context import sentiment_canary


def _ctx_row(s: Session, *, vix: float | None, fg: int | None, age_days: int = 0) -> None:
    payload = {"week_of": "2026-06-22", "macro_summary": "x"}
    if vix is not None:
        payload["vix"] = vix
    if fg is not None:
        payload["fear_greed_score"] = fg
    s.add(WeeklyMarketContextRow(
        week_of=datetime(2026, 6, 22).date(),
        payload_json=json.dumps(payload),
        created_at=datetime.now(UTC) - timedelta(days=age_days),
    ))
    s.flush()


def test_canary_trips_when_recent_context_has_no_sentiment(
    s: Session, caplog: pytest.LogCaptureFixture
) -> None:
    _ctx_row(s, vix=None, fg=None, age_days=1)
    import logging
    with caplog.at_level(logging.WARNING):
        assert sentiment_canary(s, max_age_days=7) is True
    assert "CNN scrape likely degraded" in caplog.text


def test_canary_silent_when_sentiment_present(s: Session) -> None:
    _ctx_row(s, vix=16.4, fg=37, age_days=1)
    assert sentiment_canary(s, max_age_days=7) is False


def test_canary_silent_when_latest_context_is_stale(s: Session) -> None:
    _ctx_row(s, vix=None, fg=None, age_days=30)  # older than the window
    assert sentiment_canary(s, max_age_days=7) is False


def test_canary_silent_when_no_context(s: Session) -> None:
    assert sentiment_canary(s, max_age_days=7) is False
