"""Tests for jobs/weekly_review.py — WeeklyReview dataclass and helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from investor.brokers.base import Account
from investor.config import Settings
from investor.jobs.weekly_review import (
    SuggestionAudit,
    WeeklyReview,
    _build_review,
    _filter_context_to_watchlist,
    _week_start,
    run_weekly_review_all_brokers,
    run_weekly_review_for_account,
)
from investor.models import AutoTradeState, BrokerAccount, OrderSuggestion
from investor.services.accounts import AccountInfo
from investor.services.email import FakeEmailer
from investor.services.weekly_context import WeeklyMarketContext


def _ctx(catchup: dict[str, str]) -> WeeklyMarketContext:
    return WeeklyMarketContext(
        week_of=date(2026, 6, 15),
        macro_summary="Fed held rates.",
        sector_summary="Tech led.",
        ticker_catchup=catchup,
        forward_events=["MU earnings 6/24"],
        citations=[],
    )


def test_filter_context_narrows_ticker_catchup_to_watchlist() -> None:
    ctx = _ctx({"QQQ": "q", "MU": "m", "PANW": "p"})
    # Moomoo holds PANW + QQQ, not MU
    moomoo = _filter_context_to_watchlist(ctx, ["QQQ", "PANW", "TSLA"])
    assert set(moomoo.ticker_catchup) == {"QQQ", "PANW"}
    assert "MU" not in moomoo.ticker_catchup
    # Alpaca holds MU + QQQ, not PANW
    alpaca = _filter_context_to_watchlist(ctx, ["QQQ", "MU"])
    assert set(alpaca.ticker_catchup) == {"QQQ", "MU"}
    assert "PANW" not in alpaca.ticker_catchup
    # Shared user-level fields are untouched
    assert moomoo.macro_summary == ctx.macro_summary
    assert moomoo.forward_events == ctx.forward_events


def test_filter_context_passthrough_when_nothing_to_filter() -> None:
    assert _filter_context_to_watchlist(None, ["QQQ"]) is None
    ctx = _ctx({})  # empty catch-up → returned unchanged
    assert _filter_context_to_watchlist(ctx, ["QQQ"]) is ctx
    full = _ctx({"QQQ": "q"})  # watchlist covers all keys → unchanged instance
    assert _filter_context_to_watchlist(full, ["QQQ", "MU"]) is full


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


# ── _build_review: pending-past-expires display fix ──────────────────────────



def _mock_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.get_account.return_value = Account(
        account_id="test",
        cash_usd=0.0,
        equity_usd=0.0,
        buying_power_usd=0.0,
        as_of=datetime.now(UTC),
    )
    adapter.get_positions.return_value = []
    return adapter


def _mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.broker = "alpaca_paper"
    settings.weekly_review_breakdown_top_n = 5
    settings.weekly_review_trend_weeks = 4
    return settings


def test_pending_past_expires_at_shows_expiry_note(_db_session: Session) -> None:
    """Pending suggestions whose expires_at has passed should show 'pending (expires Mon)'."""
    week_of = date(2026, 5, 25)  # Monday of current week
    _db_session.add(OrderSuggestion(
        broker_account_id=1,
        week_of=week_of,
        ticker="AAPL",
        side="buy",
        qty=3.0,
        limit_price=200.0,
        reason="test",
        status="pending",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
    ))
    _db_session.flush()

    review = _build_review(
        session=_db_session,
        adapter=_mock_adapter(),
        settings=_mock_settings(),
        week_of=week_of,
        broker_account_id=1,
    )

    assert len(review.suggestion_audits) == 1
    assert review.suggestion_audits[0].status == "pending (expires Mon)"


def test_auto_trade_mode_sourced_from_auto_trade_state_not_meta(_db_session: Session) -> None:
    """Regression: _build_review must read auto_trade_mode from auto_trade_state (per
    account), NOT the meta.auto_trade_mode key that migration d8589 deleted. Before the
    fix the deleted-key lookup always returned 'OFF', so a LIVE account read as OFF."""
    _db_session.add(AutoTradeState(broker_account_id=1, mode="LIVE"))
    _db_session.flush()

    review = _build_review(
        session=_db_session,
        adapter=_mock_adapter(),
        settings=_mock_settings(),
        week_of=date(2026, 5, 25),
        broker_account_id=1,
    )

    assert review.auto_trade_mode == "LIVE"


# ── Per-broker weekly review (Phase 4.9b: Moomoo gets its own review) ──────────

def _settings(tmp_path: Any) -> Settings:
    return Settings(
        broker="alpaca_paper", alpaca_api_key="k", alpaca_secret_key="s",
        sqlite_path=":memory:", targets_path="config/targets.yaml",
        bars_dir=str(tmp_path), email_to="t@t.com",
    )


def test_run_weekly_review_for_account_uses_nickname_subject(
    _db_session: Session, tmp_path: Any
) -> None:
    """Each account's review email is prefixed with that account's nickname."""
    emailer = FakeEmailer()
    run_weekly_review_for_account(
        _settings(tmp_path),
        _mock_adapter(),
        emailer,
        MagicMock(),                   # llm — unused here (no resolved outcomes)
        account=AccountInfo(account_ref=2, nickname="Moomoo", broker="moomoo"),
        primary_ref=1,                 # account 2 is NOT primary → no config/targets fallback
        week_of=date(2026, 5, 25),
        market_context=None,
    )
    assert len(emailer.sent) == 1
    assert emailer.sent[0]["subject"].startswith("[Moomoo] Weekly review:")


def test_all_brokers_one_email_per_account_with_shared_context(_db_session: Session) -> None:
    """run_weekly_review_all_brokers builds the user-level context ONCE and runs a per-account
    review for every active broker (so Moomoo gets its own email, not just Alpaca)."""
    for ref, nick, brk in ((1, "Alpaca paper", "alpaca"), (2, "Moomoo", "moomoo")):
        _db_session.add(BrokerAccount(
            account_ref=ref, account_id=f"a{ref}", broker=brk, mode="paper",
            nickname=nick, is_active=True, cash_usd=0.0, equity_usd=0.0,
            last_sync=datetime(2026, 5, 29, tzinfo=UTC),
            effective_from=datetime(2026, 5, 1, tzinfo=UTC),
        ))
    _db_session.commit()

    sentinel_ctx = object()
    adapters = {1: _mock_adapter(), 2: _mock_adapter()}
    with (
        patch("investor.jobs.weekly_review.datetime") as mdt,
        patch(
            "investor.jobs.weekly_review._build_and_persist_context",
            return_value=sentinel_ctx,
        ) as mctx,
        patch("investor.jobs.weekly_review.run_weekly_review_for_account") as mfor,
    ):
        mdt.now.return_value.date.return_value.weekday.return_value = 4  # Friday → guard passes
        run_weekly_review_all_brokers(
            Settings(
                broker="alpaca_paper", alpaca_api_key="k", alpaca_secret_key="s",
                sqlite_path=":memory:", targets_path="config/targets.yaml", email_to="t@t.com",
            ),
            FakeEmailer(), MagicMock(), MagicMock(), adapters, None,
        )

    assert mctx.call_count == 1                      # context built exactly once (shared)
    assert mfor.call_count == 2                      # one review per active account
    reviewed = {c.kwargs["account"].account_ref for c in mfor.call_args_list}
    assert reviewed == {1, 2}                        # both Alpaca AND Moomoo
    assert all(c.kwargs["market_context"] is sentinel_ctx for c in mfor.call_args_list)


def test_all_brokers_weekday_guard(_db_session: Session) -> None:
    """Guard: refuses to run before the week is over (Mon–Thu)."""
    with patch("investor.jobs.weekly_review.datetime") as mdt:
        mdt.now.return_value.date.return_value.weekday.return_value = 1  # Tuesday
        mdt.now.return_value.date.return_value.strftime.return_value = "Tuesday"
        with pytest.raises(RuntimeError, match="week isn't over"):
            run_weekly_review_all_brokers(
                Settings(
                    broker="alpaca_paper", alpaca_api_key="k", alpaca_secret_key="s",
                    sqlite_path=":memory:", targets_path="config/targets.yaml",
                    email_to="t@t.com",
                ),
                FakeEmailer(), MagicMock(), MagicMock(), {}, None,
            )
