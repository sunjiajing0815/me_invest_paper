"""Weekly-review reflection (plans/pre_phase5_features_design.md §4).

Deterministic outcome builder + LLM reflect_on_week (fake LLM) + guardrail check.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

from investor.jobs.weekly_review import SuggestionAudit
from investor.services.reflection import (
    SuggestionOutcome,
    build_outcomes,
)

_WEEK = date(2026, 7, 20)


def _audit(ticker="TQQQ", side="buy", status="filled", limit=66.79,
           filled=66.50, fill_status="filled") -> SuggestionAudit:
    return SuggestionAudit(
        ticker=ticker, side=side, qty=10.0, limit_price=limit, status=status,
        acted_at=datetime(2026, 7, 21, tzinfo=UTC), filled_price=filled,
        fill_status=fill_status,
    )


# ── outcome classification matrix ─────────────────────────────────────────────

def test_filled_outcome_entry_vs_current() -> None:
    out = build_outcomes(
        [_audit(status="filled", filled=66.50)],
        current_close={"TQQQ": 71.20},
        news_sentiment={},
    )
    assert len(out) == 1
    o = out[0]
    assert o.outcome == "filled"
    assert o.filled_price == 66.50
    # entry 66.50 vs current 71.20 → -6.6% below current (a good entry, in hindsight)
    assert o.entry_vs_current_pct == pytest.approx((66.50 / 71.20 - 1) * 100, abs=0.1)


def test_expired_unfilled_uses_limit_for_gap() -> None:
    out = build_outcomes(
        [_audit(status="expired", filled=None, fill_status=None)],
        current_close={"TQQQ": 71.20},
        news_sentiment={},
    )
    o = out[0]
    assert o.outcome == "expired_unfilled"
    assert o.filled_price is None
    # limit 66.79 vs current 71.20 → the level never came; measured off the limit
    assert o.entry_vs_current_pct == pytest.approx((66.79 / 71.20 - 1) * 100, abs=0.1)


def test_accepted_but_unfilled() -> None:
    out = build_outcomes(
        [_audit(status="accepted", filled=None, fill_status=None)],
        current_close={"TQQQ": 71.20}, news_sentiment={},
    )
    assert out[0].outcome == "accepted_unfilled"


def test_rejected_outcome() -> None:
    out = build_outcomes(
        [_audit(status="rejected", filled=None, fill_status=None)],
        current_close={"TQQQ": 71.20}, news_sentiment={},
    )
    assert out[0].outcome == "rejected"


def test_pending_excluded_not_resolved() -> None:
    out = build_outcomes(
        [_audit(status="pending", filled=None, fill_status=None)],
        current_close={"TQQQ": 71.20}, news_sentiment={},
    )
    assert out == []  # not a resolved outcome


def test_news_sentiment_joined() -> None:
    out = build_outcomes(
        [_audit(ticker="NVDA")],
        current_close={"NVDA": 180.0},
        news_sentiment={"NVDA": "bearish"},
    )
    assert out[0].news_sentiment == "bearish"


def test_missing_current_close_leaves_gap_none() -> None:
    out = build_outcomes([_audit()], current_close={}, news_sentiment={})
    assert out[0].current_close is None and out[0].entry_vs_current_pct is None


# ── LLM reflect_on_week (fake LLM) ────────────────────────────────────────────

from investor.services.llm import LLMResponse  # noqa: E402
from investor.services.reflection import (  # noqa: E402
    ReflectionInsightRow,
    _ReflectionInsightOut,
    _ReflectionOutput,
    load_recent_insights,
    persist_insights,
    reflect_on_week,
)


def _resp() -> LLMResponse:
    return LLMResponse(content="{}", model="claude-sonnet-4-6", prompt_hash="h",
                       input_tokens=10, output_tokens=5, cost_usd=0.01, latency_ms=100)


def _fake_llm(parsed):  # type: ignore[no-untyped-def]
    llm = MagicMock()
    llm.call.return_value = (_resp(), parsed)
    return llm


def _outcome(ticker="TQQQ", outcome="filled") -> SuggestionOutcome:
    return SuggestionOutcome(ticker=ticker, side="buy", limit_price=66.79,
                             filled_price=66.5, current_close=71.2,
                             entry_vs_current_pct=-6.6, outcome=outcome,
                             news_sentiment=None)


def test_reflect_returns_insights_and_logs(db_session) -> None:  # type: ignore[no-untyped-def]
    from investor.models import LLMCallLog
    parsed = _ReflectionOutput(insights=[
        _ReflectionInsightOut(category="limit_placement",
                              lesson="Filled buys entered ~7% below current — good margin.",
                              tickers=["TQQQ"], relation_to_prior="confirms"),
    ])
    llm = _fake_llm(parsed)
    out = reflect_on_week(llm, db_session, outcomes=[_outcome()], prior_insights=[])
    assert len(out) == 1 and out[0].category == "limit_placement"
    assert out[0].relation_to_prior == "confirms"
    # llm_call_log persisted with our purpose
    log = db_session.query(LLMCallLog).filter_by(purpose="weekly_reflection").one()
    assert log.status == "ok"


def test_reflect_empty_outcomes_skips_llm() -> None:
    llm = _fake_llm(None)
    assert reflect_on_week(llm, MagicMock(), outcomes=[], prior_insights=[]) == []
    llm.call.assert_not_called()


def test_reflect_schema_failure_returns_empty(db_session) -> None:  # type: ignore[no-untyped-def]
    llm = _fake_llm(None)  # parsed is None → validation failed
    out = reflect_on_week(llm, db_session, outcomes=[_outcome()], prior_insights=[])
    assert out == []


def test_reflect_drops_invalid_relation() -> None:
    parsed = _ReflectionOutput(insights=[
        _ReflectionInsightOut(category="sizing", lesson="x", tickers=[],
                              relation_to_prior="maybe"),  # not confirms/contradicts
    ])
    out = reflect_on_week(_fake_llm(parsed), MagicMock(), outcomes=[_outcome()],
                          prior_insights=[])
    assert out[0].relation_to_prior is None


def test_persist_and_load_prior_insights(db_session) -> None:  # type: ignore[no-untyped-def]
    ins = [ReflectionInsightRow(category="anchor", lesson="swing-lows fill best",
                                tickers=["TQQQ", "QQQ"], relation_to_prior=None)]
    persist_insights(db_session, ins, broker_account_id=61, week_of=_WEEK)
    loaded = load_recent_insights(db_session, 61, limit=8)
    assert len(loaded) == 1
    assert loaded[0].lesson == "swing-lows fill best"
    assert loaded[0].tickers == ["TQQQ", "QQQ"]
    # scoped per account
    assert load_recent_insights(db_session, 62, limit=8) == []


def test_prompt_has_guardrail_rules() -> None:
    from investor.services.llm import load_prompt
    p = load_prompt("weekly_reflection_v1.txt").lower()
    assert "must not recommend" in p
    assert "must not predict prices" in p or "price target" in p
    assert "empty" in p  # instruct empty list when no lesson


# ── email rendering ───────────────────────────────────────────────────────────

def _weekly_review(reflection, outcomes):  # type: ignore[no-untyped-def]
    from investor.jobs.weekly_review import WeeklyReview
    from investor.services.daily_report import AccountSnapshot
    return WeeklyReview(
        week_of=_WEEK, account=AccountSnapshot("alpaca", "paper", 1000.0, 5000.0),
        realized_pnl_usd=0.0, suggestion_audits=[], gap_rows=[], material_news={},
        auto_trade_mode="OFF", promotions_this_week=[], kill_switches_this_week=[],
        executions_this_week=0, preview_suggestions=[],
        reflection=reflection, outcomes=outcomes,
    )


def _render_review(reflection, outcomes):  # type: ignore[no-untyped-def]
    from investor.services.render import render_template
    return render_template("weekly_review.html.j2",
                           review=_weekly_review(reflection, outcomes), ticker_names={})


def test_review_email_renders_reflection() -> None:
    ins = [ReflectionInsightRow(category="limit_placement",
                                lesson="Filled buys entered ~7% below current.",
                                tickers=["TQQQ"], relation_to_prior="confirms")]
    html = _render_review(ins, [_outcome()])
    assert "Reflection — Lessons from This Week" in html
    assert "Filled buys entered ~7% below current." in html
    assert "limit_placement" in html
    assert "confirms a prior week" in html
    assert "not trade advice" in html


def test_review_email_no_reflection_section_when_empty() -> None:
    assert "Reflection — Lessons" not in _render_review(None, None)
