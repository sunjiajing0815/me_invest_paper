"""Smoke tests for services/weekly_review_metrics.py."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.models import (
    Base,
    OrderExecution,
    OrderSuggestion,
    PositionsSnapshot,
    TargetAllocation,
)
from investor.services.weekly_review_metrics import (
    compute_4_week_trend,
    compute_allocation_drift,
    compute_order_flow,
    compute_order_funnel,
)

# Test week
_MON = date(2026, 5, 25)   # Monday
_FRI = date(2026, 5, 30)   # Friday


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _sug(
    session: Session,
    *,
    ticker: str = "VOO",
    side: str = "buy",
    qty: float = 10.0,
    limit_price: float = 500.0,
    status: str = "pending",
    week_of: date = _MON,
    broker_account_id: int = 1,
) -> int:
    """Insert an OrderSuggestion and return its id."""
    row = OrderSuggestion(
        broker_account_id=broker_account_id,
        week_of=week_of,
        ticker=ticker,
        side=side,
        qty=qty,
        limit_price=limit_price,
        reason="test",
        status=status,
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    session.add(row)
    session.flush()
    return row.id  # type: ignore[return-value]


def _exec(
    session: Session,
    *,
    suggestion_id: int,
    ticker: str = "VOO",
    side: str = "buy",
    status: str = "filled",
    filled_qty: float = 10.0,
    filled_price: float = 500.0,
    limit_price: float = 500.0,
    dry_run: bool = False,
    broker_account_id: int = 1,
) -> OrderExecution:
    """Insert an OrderExecution."""
    row = OrderExecution(
        broker_account_id=broker_account_id,
        suggestion_id=suggestion_id,
        ticker=ticker,
        side=side,
        submitted_qty=filled_qty,
        filled_qty=filled_qty,
        limit_price=limit_price,
        filled_price=filled_price if status == "filled" else None,
        filled_at=datetime.now(UTC) if status == "filled" else None,
        broker="alpaca",
        status=status,
        dry_run=dry_run,
        match_method="auto_trade_placed",
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def _snap(
    session: Session,
    *,
    ts: datetime,
    ticker: str,
    market_value: float,
    weight_pct: float = 10.0,
    broker_account_id: int = 1,
) -> PositionsSnapshot:
    """Insert a PositionsSnapshot row."""
    row = PositionsSnapshot(
        broker_account_id=broker_account_id,
        ts=ts,
        ticker=ticker,
        market_value=market_value,
        weight_pct=weight_pct,
        qty=1.0,
        avg_cost=market_value,
        account_id=None,
    )
    session.add(row)
    session.flush()
    return row


def _target(
    session: Session,
    *,
    ticker: str,
    target_pct: float,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    band_low_pct: float | None = None,
    band_high_pct: float | None = None,
    broker_account_id: int = 1,
) -> TargetAllocation:
    """Insert a TargetAllocation row."""
    if effective_from is None:
        effective_from = datetime(2026, 1, 1, tzinfo=UTC)
    row = TargetAllocation(
        broker_account_id=broker_account_id,
        ticker=ticker,
        target_pct=target_pct,
        band_low_pct=band_low_pct if band_low_pct is not None else (target_pct - 5),
        band_high_pct=band_high_pct if band_high_pct is not None else (target_pct + 5),
        effective_from=effective_from,
        effective_to=effective_to,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Test 1 — funnel empty week
# ---------------------------------------------------------------------------

def test_funnel_empty_week(session: Session) -> None:
    """No rows at all → OrderFunnel with all zeros, no crash."""
    funnel = compute_order_funnel(session, mon=_MON, fri=_FRI, broker_account_id=1)
    assert funnel.suggested == 0
    assert funnel.accepted == 0
    assert funnel.routed_live == 0
    assert funnel.dry_run_count == 0


# ---------------------------------------------------------------------------
# Test 2 — funnel typical week
# ---------------------------------------------------------------------------

def test_funnel_typical_week(session: Session) -> None:
    """Insert 3 suggestions (1 accepted+filled, 1 rejected, 1 expired). Verify counts."""
    sug1 = _sug(session, status="accepted")
    _sug(session, ticker="AAPL", status="rejected")
    _sug(session, ticker="QQQ", status="expired")
    _exec(session, suggestion_id=sug1, status="filled", dry_run=False)

    funnel = compute_order_funnel(session, mon=_MON, fri=_FRI, broker_account_id=1)
    assert funnel.suggested == 3
    assert funnel.accepted == 1
    assert funnel.routed_live == 1
    assert funnel.filled_live == 1
    assert funnel.rejected == 1
    assert funnel.expired == 1
    assert funnel.accepted_not_routed == 0


# ---------------------------------------------------------------------------
# Test 3 — funnel dry_run only
# ---------------------------------------------------------------------------

def test_funnel_dry_run_only(session: Session) -> None:
    """1 suggestion accepted with only a dry_run execution → routed_live=0, dry_run_count=1."""
    sug = _sug(session, status="accepted")
    _exec(session, suggestion_id=sug, status="filled", dry_run=True)

    funnel = compute_order_funnel(session, mon=_MON, fri=_FRI, broker_account_id=1)
    assert funnel.routed_live == 0
    assert funnel.filled_live == 0
    assert funnel.dry_run_count == 1


# ---------------------------------------------------------------------------
# Test 4 — funnel accepted not routed
# ---------------------------------------------------------------------------

def test_funnel_accepted_not_routed(session: Session) -> None:
    """1 suggestion accepted but no execution row → accepted_not_routed=1."""
    _sug(session, status="accepted")

    funnel = compute_order_funnel(session, mon=_MON, fri=_FRI, broker_account_id=1)
    assert funnel.accepted_not_routed == 1
    assert funnel.routed_live == 0


# ---------------------------------------------------------------------------
# Test 5 — drift sign: under target, moved closer
# ---------------------------------------------------------------------------

def test_drift_sign_under_target_moved_closer(session: Session) -> None:
    """Ticker VOO under-target Mon→Fri move closer. drift_pp>0, moved_toward_target=True."""
    _target(session, ticker="VOO", target_pct=30.0)
    mon_ts = datetime(_MON.year, _MON.month, _MON.day, 16, 0, tzinfo=UTC)
    fri_ts = datetime(_FRI.year, _FRI.month, _FRI.day, 16, 0, tzinfo=UTC)

    # weight_pct is already a % of TOTAL equity (incl. cash); the drift table uses it directly.
    _snap(session, ts=mon_ts, ticker="VOO", market_value=200.0, weight_pct=20.0)
    _snap(session, ts=fri_ts, ticker="VOO", market_value=250.0, weight_pct=25.0)

    drift = compute_allocation_drift(session, mon=_MON, fri=_FRI, broker_account_id=1)
    voo = next(r for r in drift if r.ticker == "VOO")

    assert voo.current_pct_mon == pytest.approx(20.0, abs=0.1)
    assert voo.current_pct_fri == pytest.approx(25.0, abs=0.1)
    assert voo.drift_pp > 0
    assert voo.moved_toward_target is True


# ---------------------------------------------------------------------------
# Test 6 — drift over-correction
# ---------------------------------------------------------------------------

def test_drift_over_correction(session: Session) -> None:
    """Ticker goes from -2pp gap to +1pp gap. |1| < |2| → moved_toward_target=True."""
    _target(session, ticker="VOO", target_pct=30.0)
    mon_ts = datetime(_MON.year, _MON.month, _MON.day, 16, 0, tzinfo=UTC)
    fri_ts = datetime(_FRI.year, _FRI.month, _FRI.day, 16, 0, tzinfo=UTC)

    # Monday 28% (gap -2pp), Friday 31% (gap +1pp) — |1| < |2| → moved closer
    _snap(session, ts=mon_ts, ticker="VOO", market_value=280.0, weight_pct=28.0)
    _snap(session, ts=fri_ts, ticker="VOO", market_value=310.0, weight_pct=31.0)

    drift = compute_allocation_drift(session, mon=_MON, fri=_FRI, broker_account_id=1)
    voo = next(r for r in drift if r.ticker == "VOO")

    assert abs(voo.gap_pct_fri) < abs(voo.gap_pct_mon)
    assert voo.moved_toward_target is True


# ---------------------------------------------------------------------------
# Test 7 — drift Monday fallback
# ---------------------------------------------------------------------------

def test_drift_monday_fallback(session: Session) -> None:
    """No snapshot on Monday; prior Friday used instead → monday_is_fallback=True."""
    _target(session, ticker="VOO", target_pct=30.0)
    prior_fri = _MON - timedelta(days=3)  # prior Friday (2026-05-22)
    prior_fri_ts = datetime(prior_fri.year, prior_fri.month, prior_fri.day, 16, 0, tzinfo=UTC)
    fri_ts = datetime(_FRI.year, _FRI.month, _FRI.day, 16, 0, tzinfo=UTC)

    _snap(session, ts=prior_fri_ts, ticker="VOO", market_value=300.0)
    _snap(session, ts=fri_ts, ticker="VOO", market_value=310.0)

    drift = compute_allocation_drift(session, mon=_MON, fri=_FRI, broker_account_id=1)
    assert len(drift) > 0
    assert drift[0].monday_is_fallback is True


# ---------------------------------------------------------------------------
# Test 8 — drift targets changed midweek
# ---------------------------------------------------------------------------

def test_drift_targets_changed_midweek(session: Session) -> None:
    """TargetAllocation row effective_from=Wednesday → targets_changed_midweek=True on all rows."""
    wed = _MON + timedelta(days=2)
    wed_dt = datetime(wed.year, wed.month, wed.day, tzinfo=UTC)
    _target(session, ticker="VOO", target_pct=30.0, effective_from=wed_dt)

    mon_ts = datetime(_MON.year, _MON.month, _MON.day, 16, 0, tzinfo=UTC)
    fri_ts = datetime(_FRI.year, _FRI.month, _FRI.day, 16, 0, tzinfo=UTC)
    _snap(session, ts=mon_ts, ticker="VOO", market_value=300.0)
    _snap(session, ts=fri_ts, ticker="VOO", market_value=300.0)

    drift = compute_allocation_drift(session, mon=_MON, fri=_FRI, broker_account_id=1)
    assert all(r.targets_changed_midweek for r in drift)


# ---------------------------------------------------------------------------
# Test 9 — weekday guard raises on Wednesday
# ---------------------------------------------------------------------------

def test_weekday_guard_raises_on_wednesday() -> None:
    """Mock datetime.now(UTC).date() to return a Wednesday → RuntimeError raised."""
    from datetime import date as _date
    from unittest.mock import MagicMock, patch

    import investor.jobs.weekly_review as wr_mod

    wednesday = _date(2026, 5, 27)  # weekday=2 (Wednesday)

    mock_now_result = MagicMock()
    mock_now_result.date.return_value = wednesday

    with patch("investor.jobs.weekly_review.datetime") as mock_dt:
        mock_dt.now.return_value = mock_now_result

        with pytest.raises(RuntimeError, match="week isn't over yet"):
            wr_mod.run_weekly_review(
                settings=MagicMock(),
                adapter=MagicMock(),
                emailer=MagicMock(),
                llm=MagicMock(),
                tavily=MagicMock(),
            )


# ---------------------------------------------------------------------------
# Test 10 — order flow zeros when no executions
# ---------------------------------------------------------------------------

def test_order_flow_zeros_when_no_executions(session: Session) -> None:
    """No execution rows → OrderFlow with all float zeros, no crash."""
    flow = compute_order_flow(session, mon=_MON, fri=_FRI, broker_account_id=1)
    assert flow.buy_routed_usd == 0.0
    assert flow.sell_routed_usd == 0.0
    assert flow.buy_filled_usd == 0.0
    assert flow.sell_filled_usd == 0.0
    assert flow.distinct_tickers == 0


# ---------------------------------------------------------------------------
# Test 11 — trend filled_live includes partial fills
# ---------------------------------------------------------------------------

def test_trend_filled_live_includes_partial_fills(session: Session) -> None:
    """A partially_filled execution should count in trend filled_live."""
    current_fri = _FRI
    sug_id = _sug(session, status="accepted", week_of=_MON)
    row = OrderExecution(
        broker_account_id=1,
        suggestion_id=sug_id,
        ticker="VOO",
        side="buy",
        submitted_qty=10.0,
        filled_qty=5.0,
        limit_price=500.0,
        filled_price=None,
        filled_at=None,
        broker="alpaca",
        broker_order_id="test-partial-001",
        status="partially_filled",
        dry_run=False,
        match_method="auto_trade_placed",
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()

    trend_rows = compute_4_week_trend(session, current_fri=current_fri, broker_account_id=1)
    assert len(trend_rows) >= 1
    assert trend_rows[-1].filled_live == 1


# ---------------------------------------------------------------------------
# Multi-broker scoping (regression: duplicate drift rows for shared tickers)
# ---------------------------------------------------------------------------

def test_allocation_drift_scoped_to_one_account_no_duplicates(session: Session) -> None:
    """A ticker targeted in TWO accounts must yield exactly ONE drift row for the scoped
    account — not one per account. This is the duplicate-entries bug: alloc_drift.sql had
    no broker_account_id filter, so the targets CTE returned the ticker once per account."""
    mon_ts = datetime(2026, 5, 25, 16, 0, tzinfo=UTC)
    fri_ts = datetime(2026, 5, 29, 16, 0, tzinfo=UTC)
    # QQQ targeted in both accounts, with DIFFERENT target %
    _target(session, ticker="QQQ", target_pct=25.0, broker_account_id=1)
    _target(session, ticker="QQQ", target_pct=30.0, broker_account_id=2)
    for acct, wt_mon, wt_fri in ((1, 15.0, 16.0), (2, 40.0, 42.0)):
        _snap(session, ts=mon_ts, ticker="QQQ", market_value=1000.0, weight_pct=wt_mon,
              broker_account_id=acct)
        _snap(session, ts=fri_ts, ticker="QQQ", market_value=1100.0, weight_pct=wt_fri,
              broker_account_id=acct)

    drift = compute_allocation_drift(session, mon=_MON, fri=_FRI, broker_account_id=1)
    qqq = [d for d in drift if d.ticker == "QQQ"]
    assert len(qqq) == 1                     # ONE row, not two (the bug produced two)
    assert qqq[0].target_pct == 25.0         # account 1's target, not account 2's 30.0
    assert qqq[0].current_pct_mon == pytest.approx(15.0)  # account 1's weight, not account 2's 40


def test_funnel_scoped_to_one_account(session: Session) -> None:
    """Account 2's suggestions must not count toward account 1's funnel."""
    _sug(session, status="accepted", broker_account_id=1)
    _sug(session, ticker="AAPL", status="accepted", broker_account_id=2)
    _sug(session, ticker="MSFT", status="accepted", broker_account_id=2)

    funnel = compute_order_funnel(session, mon=_MON, fri=_FRI, broker_account_id=1)
    assert funnel.suggested == 1   # only account 1's, not the 2 from account 2
    assert funnel.accepted == 1


def test_allocation_drift_dedups_multiple_same_day_snapshots(session: Session) -> None:
    """Multiple syncs on the same day (3 distinct ts) must NOT fan out the drift rows —
    the query uses the latest snapshot batch (one ts), so each ticker appears once, not
    once per (mon_snap x fri_snap) same-day row (the remaining duplicate-entries bug)."""
    _target(session, ticker="QQQ", target_pct=25.0)
    _target(session, ticker="VOO", target_pct=40.0)
    # 3 syncs on Monday and 3 on Friday, each a full snapshot of both tickers
    for h in (9, 13, 16):
        for tk, mv_mon, mv_fri in (("QQQ", 1000.0, 1100.0), ("VOO", 2000.0, 2200.0)):
            _snap(session, ts=datetime(2026, 5, 25, h, tzinfo=UTC), ticker=tk, market_value=mv_mon)
            _snap(session, ts=datetime(2026, 5, 29, h, tzinfo=UTC), ticker=tk, market_value=mv_fri)

    drift = compute_allocation_drift(session, mon=_MON, fri=_FRI, broker_account_id=1)
    from collections import Counter
    counts = Counter(d.ticker for d in drift)
    assert counts["QQQ"] == 1, f"QQQ duplicated: {counts['QQQ']}"  # was 3x3=9 before the fix
    assert counts["VOO"] == 1, f"VOO duplicated: {counts['VOO']}"
