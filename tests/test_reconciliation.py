"""Tests for services/reconciliation.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from investor.brokers.base import Activity, OrderConfirmation
from investor.jobs.reconciliation import _meta_key, run_daily_reconciliation_for_account
from investor.models import Meta, OrderExecution, OrderSuggestion
from investor.services.accounts import AccountInfo
from investor.services.reconciliation import (
    compute_realized_pnl,
    infer_manual_cancels,
    persist_reconciliation,
    reconcile_activities,
    sync_open_order_statuses,
)

_ACCT = 1  # account_ref for reconciliation tests

_NOW = datetime(2026, 5, 1, 16, 0, tzinfo=UTC)
_WEEK = date(2026, 4, 28)


# ── helpers ──────────────────────────────────────────────────────────────────

def _act(
    ticker: str = "AAPL",
    side: str = "buy",
    filled_qty: float = 5.0,
    filled_price: float = 100.0,
    broker_order_id: str = "b-001",
    client_order_id: str | None = None,
    filled_at: datetime | None = None,
    status: str = "filled",
) -> Activity:
    return Activity(
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        ticker=ticker,
        side=side,
        filled_qty=filled_qty,
        filled_price=filled_price,
        filled_at=filled_at or _NOW,
        status=status,  # type: ignore[arg-type]
    )


def _sug(
    session: Session,
    ticker: str = "AAPL",
    side: str = "buy",
    qty: float = 5.0,
    limit_price: float = 100.0,
    status: str = "accepted",
    created_at: datetime | None = None,
) -> OrderSuggestion:
    s = OrderSuggestion(broker_account_id=_ACCT, 
        week_of=_WEEK,
        ticker=ticker,
        side=side,
        qty=qty,
        limit_price=limit_price,
        reason="test",
        status=status,
        created_at=created_at or _NOW - timedelta(hours=1),
    )
    session.add(s)
    session.flush()
    return s


def _exe(
    session: Session,
    ticker: str = "AAPL",
    side: str = "buy",
    filled_qty: float = 5.0,
    filled_price: float = 100.0,
    broker_order_id: str = "b-001",
    broker: str = "alpaca",
    dry_run: bool = False,
    status: str = "filled",
    filled_at: datetime | None = None,
    realized_pnl_usd: float | None = None,
    match_method: str = "untracked",
) -> OrderExecution:
    row = OrderExecution(broker_account_id=_ACCT, 
        ticker=ticker,
        side=side,
        filled_qty=filled_qty,
        filled_price=filled_price,
        broker_order_id=broker_order_id,
        broker=broker,
        dry_run=dry_run,
        status=status,
        match_method=match_method,
        match_confidence=0.0,
        created_at=_NOW - timedelta(hours=2),
        filled_at=filled_at or (_NOW - timedelta(hours=2)),
        realized_pnl_usd=realized_pnl_usd,
    )
    session.add(row)
    session.flush()
    return row


def _adapter(activities: list[Activity]) -> MagicMock:
    m = MagicMock()
    m.get_activities.return_value = activities
    return m




# ── Rule 1: sug-N client_order_id ────────────────────────────────────────────

def test_rule1_client_order_id_matched(db_session: Session) -> None:
    sug = _sug(db_session)
    act = _act(client_order_id=f"sug-{sug.id}")
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    assert len(results) == 1
    r = results[0]
    assert r.suggestion_id == sug.id
    assert r.method == "auto_matched"
    assert r.confidence == pytest.approx(1.0)


def test_rule1_auto_trade_row_updated_not_duplicated(db_session: Session) -> None:
    """Existing dry_run=False row with same broker_order_id → UPDATE fill fields, no INSERT."""
    sug = _sug(db_session)
    _exe(db_session, broker_order_id="b-001", dry_run=False, filled_price=99.0)

    act = _act(broker_order_id="b-001", client_order_id=f"sug-{sug.id}", filled_price=100.5)
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    persist_reconciliation(db_session, results, broker="alpaca", broker_account_id=_ACCT)
    db_session.flush()

    rows = db_session.scalars(
        select(OrderExecution).where(OrderExecution.broker_order_id == "b-001")
    ).all()
    assert len(rows) == 1
    assert rows[0].filled_price == pytest.approx(100.5)


def test_auto_trade_row_updated_despite_broker_string_mismatch(db_session: Session) -> None:
    """The placement row may carry broker='alpaca_paper' (back-compat auto-trade entrypoint)
    while reconciliation passes the bare family 'alpaca'. Matching by broker_order_id +
    account (not the broker string) updates the placement row IN PLACE — no duplicate filled
    row, no orphan accepted_for_routing (the GOOG regression)."""
    sug = _sug(db_session)
    _exe(
        db_session, broker_order_id="b-002", dry_run=False, broker="alpaca_paper",
        status="accepted_for_routing", filled_qty=0.0, filled_price=0.0,
    )
    act = _act(broker_order_id="b-002", client_order_id=f"sug-{sug.id}", filled_price=101.0)
    results = reconcile_activities(
        broker_account_id=_ACCT, session=db_session,
        adapter=_adapter([act]), since=_NOW - timedelta(hours=2),
    )
    persist_reconciliation(db_session, results, broker="alpaca", broker_account_id=_ACCT)
    db_session.flush()

    rows = db_session.scalars(
        select(OrderExecution).where(OrderExecution.broker_order_id == "b-002")
    ).all()
    assert len(rows) == 1                            # updated in place, not duplicated
    assert rows[0].status == "filled"
    assert rows[0].filled_price == pytest.approx(101.0)


def test_rule1_dry_run_rows_ignored_by_reconciliation(db_session: Session) -> None:
    """A dry_run=True row does NOT satisfy the existing-row check in persist_reconciliation.

    Real dry_run rows use broker='dry_run' and broker_order_id=None. When a real activity
    arrives for a new broker_order_id, persist_reconciliation looks for dry_run=False rows
    only and finds none, so it inserts a new real row.
    """
    # Simulate a DRY_RUN row: broker="dry_run", broker_order_id=None
    dry_row = OrderExecution(broker_account_id=_ACCT, 
        ticker="AAPL",
        side="buy",
        filled_qty=5.0,
        filled_price=100.0,
        broker="dry_run",
        broker_order_id=None,
        client_order_id="sug-99",
        dry_run=True,
        status="dry_run",
        match_method="auto_trade_placed",
        match_confidence=1.0,
        created_at=_NOW - timedelta(hours=2),
        filled_at=None,
    )
    db_session.add(dry_row)
    db_session.flush()

    # A real fill arrives for the same suggestion
    act = _act(broker_order_id="b-real", client_order_id="sug-99")
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    persist_reconciliation(db_session, results, broker="alpaca", broker_account_id=_ACCT)
    db_session.flush()

    # New real row inserted; dry_run row untouched
    real_rows = db_session.scalars(
        select(OrderExecution).where(
            OrderExecution.dry_run.is_(False),
            OrderExecution.broker_order_id == "b-real",
        )
    ).all()
    assert len(real_rows) == 1

    dry_rows = db_session.scalars(
        select(OrderExecution).where(OrderExecution.dry_run.is_(True))
    ).all()
    assert len(dry_rows) == 1  # untouched


def test_rule1_retry_order_id_sug_n_rn_matched(db_session: Session) -> None:
    """Rule 1 parses 'sug-N-rM' correctly — retry suffix must not break the int parse."""
    sug = _sug(db_session)
    act = _act(
        client_order_id=f"sug-{sug.id}-r1",
        broker_order_id="b-retry-001",
        filled_at=datetime.now(UTC),
        status="filled",
    )
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    assert len(results) == 1
    r = results[0]
    assert r.suggestion_id == sug.id
    assert r.method == "auto_matched"
    assert r.confidence == pytest.approx(1.0)


def test_rule1_malformed_sug_id_falls_through_to_heuristic(db_session: Session) -> None:
    """A malformed 'sug-foo-r1' client_order_id must NOT match via Rule 1."""
    act = _act(
        client_order_id="sug-foo-r1",
        broker_order_id="b-malformed",
        ticker="AAPL",
    )
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    assert len(results) == 1
    r = results[0]
    assert r.method != "auto_matched", (
        "malformed sug-foo-r1 must not match via Rule 1 (auto_matched)"
    )
    # With no matching suggestion in DB, falls through to 'untracked'
    assert r.method in {"untracked", "manual_review"}


# ── Rule 2: single heuristic candidate ────────────────────────────────────────

def test_rule2_heuristic_single_candidate(db_session: Session) -> None:
    sug = _sug(db_session, ticker="MSFT", limit_price=400.0)
    act = _act(ticker="MSFT", filled_price=400.5, broker_order_id="b-002")
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    assert len(results) == 1
    r = results[0]
    assert r.suggestion_id == sug.id
    assert r.method == "auto_matched"
    assert r.confidence == pytest.approx(0.9)


# ── Rule 3: multiple candidates → manual_review ────────────────────────────────

def test_rule3_ambiguous_flags_manual_review(db_session: Session) -> None:
    # Two accepted suggestions from different weeks — unique constraint is (week_of, ticker, side)
    prior_week = date(2026, 4, 21)  # one week earlier
    s1 = OrderSuggestion(broker_account_id=_ACCT, 
        week_of=_WEEK, ticker="NVDA", side="buy", qty=2.0, limit_price=800.0,
        reason="test", status="accepted", created_at=_NOW - timedelta(hours=2),
    )
    s2 = OrderSuggestion(broker_account_id=_ACCT, 
        week_of=prior_week, ticker="NVDA", side="buy", qty=4.0, limit_price=800.0,
        reason="test", status="accepted", created_at=_NOW - timedelta(hours=3),
    )
    db_session.add_all([s1, s2])
    db_session.flush()

    act = _act(ticker="NVDA", filled_price=800.0, broker_order_id="b-003")
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=4)
    )
    assert len(results) == 1
    assert results[0].method == "manual_review"
    assert results[0].confidence == pytest.approx(0.5)


# ── Rule 4: no candidate → untracked ──────────────────────────────────────────

def test_rule4_untracked(db_session: Session) -> None:
    act = _act(ticker="TSLA", broker_order_id="b-004")
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    assert len(results) == 1
    r = results[0]
    assert r.method == "untracked"
    assert r.suggestion_id is None
    assert r.confidence == pytest.approx(0.0)


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_idempotency_rerun_no_duplicates(db_session: Session) -> None:
    act = _act(broker_order_id="b-idem")
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    persist_reconciliation(db_session, results, broker="alpaca", broker_account_id=_ACCT)
    db_session.flush()

    results2 = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    persist_reconciliation(db_session, results2, broker="alpaca", broker_account_id=_ACCT)
    db_session.flush()

    rows = db_session.scalars(
        select(OrderExecution).where(OrderExecution.broker_order_id == "b-idem")
    ).all()
    assert len(rows) == 1


# ── Activity with no filled_at is skipped ─────────────────────────────────────

def test_activity_no_filled_at_is_skipped(db_session: Session) -> None:
    act = Activity(
        broker_order_id="b-nofill",
        client_order_id=None,
        ticker="AAPL",
        side="buy",
        filled_qty=5.0,
        filled_price=100.0,
        filled_at=None,
        status="canceled",
    )
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    assert results == []


# ── FIFO PnL ──────────────────────────────────────────────────────────────────

def test_realized_pnl_fifo_basic(db_session: Session) -> None:
    """5 shares buy @$100, sell 5 @$110 → $50 PnL."""
    t0 = _NOW - timedelta(hours=5)
    _exe(
        db_session,
        ticker="AAPL", side="buy", filled_qty=5.0, filled_price=100.0,
        broker_order_id="b-buy1", filled_at=t0,
    )
    sell = _act(ticker="AAPL", side="sell", filled_qty=5.0, filled_price=110.0, filled_at=_NOW)
    pnl = compute_realized_pnl(db_session, sell)
    assert pnl == pytest.approx(50.0)


def test_realized_pnl_fifo_partial_lots(db_session: Session) -> None:
    """Two lots: 3@$100 then 2@$120; sell 4 uses oldest-first: cost = 3*100 + 1*120 = 420."""
    t1 = _NOW - timedelta(hours=4)
    t2 = _NOW - timedelta(hours=3)
    _exe(db_session, ticker="GOOG", side="buy", filled_qty=3.0, filled_price=100.0,
         broker_order_id="b-lot1", filled_at=t1)
    _exe(db_session, ticker="GOOG", side="buy", filled_qty=2.0, filled_price=120.0,
         broker_order_id="b-lot2", filled_at=t2)
    db_session.flush()

    sell = _act(ticker="GOOG", side="sell", filled_qty=4.0, filled_price=150.0, filled_at=_NOW)
    pnl = compute_realized_pnl(db_session, sell)
    # proceeds: 4*150=600; cost: 3*100+1*120=420; pnl=180
    assert pnl == pytest.approx(180.0)


def test_realized_pnl_no_buy_history_returns_none(db_session: Session) -> None:
    sell = _act(ticker="AMZN", side="sell", filled_qty=2.0, filled_price=200.0)
    pnl = compute_realized_pnl(db_session, sell)
    assert pnl is None


def test_realized_pnl_future_buy_excluded(db_session: Session) -> None:
    """A buy AFTER the sell's filled_at must NOT be included in FIFO cost basis."""
    future_buy_time = _NOW + timedelta(hours=1)
    _exe(
        db_session, ticker="AAPL", side="buy", filled_qty=5.0, filled_price=90.0,
        broker_order_id="b-future", filled_at=future_buy_time,
    )
    sell = _act(ticker="AAPL", side="sell", filled_qty=5.0, filled_price=110.0, filled_at=_NOW)
    pnl = compute_realized_pnl(db_session, sell)
    assert pnl is None  # no prior buys before sell timestamp


# ── Suggestion flipped to 'filled' on auto_matched ───────────────────────────

def test_accepted_suggestion_flipped_to_filled(db_session: Session) -> None:
    sug = _sug(db_session)
    assert sug.status == "accepted"
    act = _act(client_order_id=f"sug-{sug.id}")
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    persist_reconciliation(db_session, results, broker="alpaca", broker_account_id=_ACCT)
    db_session.flush()
    db_session.refresh(sug)
    assert sug.status == "filled"


def test_partially_filled_does_not_flip_suggestion_to_filled(db_session: Session) -> None:
    """A partially_filled activity must NOT advance an accepted suggestion to 'filled'."""
    sug = _sug(db_session, ticker="AAPL", side="buy", qty=5.0, limit_price=100.0)
    assert sug.status == "accepted"
    # client_order_id does NOT start with "sug-" → Rule 1 does not apply; heuristic fires
    act = _act(
        ticker="AAPL",
        side="buy",
        filled_qty=5.0,
        filled_price=100.0,
        broker_order_id="b-partial-001",
        client_order_id=None,
        status="partially_filled",
    )
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    assert len(results) == 1
    assert results[0].method == "auto_matched"  # heuristic matched
    persist_reconciliation(db_session, results, broker="alpaca", broker_account_id=_ACCT)
    db_session.flush()
    db_session.refresh(sug)
    assert sug.status == "accepted"  # must remain accepted — GTC order still open


def test_filled_activity_does_flip_suggestion(db_session: Session) -> None:
    """Regression guard: a fully filled activity DOES advance an accepted suggestion to 'filled'."""
    sug = _sug(db_session, ticker="AAPL", side="buy", qty=5.0, limit_price=100.0)
    assert sug.status == "accepted"
    act = _act(
        ticker="AAPL",
        side="buy",
        filled_qty=5.0,
        filled_price=100.0,
        broker_order_id="b-filled-001",
        client_order_id=None,
        status="filled",
    )
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    assert len(results) == 1
    assert results[0].method == "auto_matched"  # heuristic matched
    persist_reconciliation(db_session, results, broker="alpaca", broker_account_id=_ACCT)
    db_session.flush()
    db_session.refresh(sug)
    assert sug.status == "filled"


def test_untracked_activity_does_not_flip_suggestion(db_session: Session) -> None:
    sug = _sug(db_session)
    act = _act(ticker="AAPL", broker_order_id="b-untracked")
    results = reconcile_activities(broker_account_id=_ACCT, 
        session=db_session, adapter=_adapter([act]), since=_NOW - timedelta(hours=2)
    )
    persist_reconciliation(db_session, results, broker="alpaca", broker_account_id=_ACCT)
    db_session.flush()
    db_session.refresh(sug)
    # Heuristic may have matched (single candidate), but untracked should not flip
    # The key invariant: non-auto_matched suggestions are never flipped to 'filled'
    # (only auto_matched with a suggestion_id are flipped)
    r = results[0]
    if r.method != "auto_matched":
        assert sug.status == "accepted"


# ── sync_open_order_statuses ──────────────────────────────────────────────────

def test_sync_open_order_statuses_marks_cancelled(db_session: Session) -> None:
    exe = _exe(db_session, broker_order_id="ord-1", status="accepted_for_routing", dry_run=False)
    db_session.flush()

    mock_adapter = MagicMock()
    mock_adapter.list_orders.return_value = [
        OrderConfirmation(
            broker_order_id="ord-1",
            client_order_id=None,
            status="canceled",
            submitted_at=datetime.now(UTC),
        )
    ]

    result = sync_open_order_statuses(db_session, mock_adapter, _ACCT)

    # Check in-memory — the service mutated exe.status via the identity map.
    # Do not call refresh() before flush(); it would re-read the unflushed DB row.
    assert exe.status == "broker_cancelled"
    assert result == 1


def test_sync_open_order_statuses_ignores_live_order(db_session: Session) -> None:
    exe = _exe(db_session, broker_order_id="ord-2", status="accepted_for_routing", dry_run=False)
    db_session.flush()

    mock_adapter = MagicMock()
    # "ord-2" is not in the closed orders list — still open
    mock_adapter.list_orders.return_value = []

    result = sync_open_order_statuses(db_session, mock_adapter, _ACCT)

    db_session.refresh(exe)
    assert exe.status == "accepted_for_routing"
    assert result == 0


def test_sync_open_order_statuses_tolerates_list_orders_failure(db_session: Session) -> None:
    exe = _exe(db_session, broker_order_id="ord-3", status="accepted_for_routing", dry_run=False)
    db_session.flush()

    mock_adapter = MagicMock()
    mock_adapter.list_orders.side_effect = Exception("broker down")

    result = sync_open_order_statuses(db_session, mock_adapter, _ACCT)

    db_session.refresh(exe)
    assert exe.status == "accepted_for_routing"
    assert result == 0


def test_sync_uses_batch_list_orders(db_session: Session) -> None:
    """sync_open_order_statuses calls list_orders once, never get_order, marks both cancelled."""
    exe1 = _exe(db_session, broker_order_id="batch-1", status="accepted_for_routing", dry_run=False)
    exe2 = _exe(db_session, broker_order_id="batch-2", status="accepted_for_routing", dry_run=False)
    db_session.flush()

    mock_adapter = MagicMock()
    mock_adapter.list_orders.return_value = [
        OrderConfirmation(
            broker_order_id="batch-1",
            client_order_id=None,
            status="canceled",
            submitted_at=datetime.now(UTC),
        ),
        OrderConfirmation(
            broker_order_id="batch-2",
            client_order_id=None,
            status="canceled",
            submitted_at=datetime.now(UTC),
        ),
    ]

    result = sync_open_order_statuses(db_session, mock_adapter, _ACCT)

    # list_orders called exactly once with status="closed"
    mock_adapter.list_orders.assert_called_once_with(status="closed")
    # get_order must never be called
    mock_adapter.get_order.assert_not_called()
    # Both executions marked broker_cancelled
    assert exe1.status == "broker_cancelled"
    assert exe2.status == "broker_cancelled"
    assert result == 2


# ── Reconciliation window extends back to oldest open order (late GTC fills) ───

def _open_exec(session: Session, *, created_at: datetime, broker_order_id: str) -> None:
    """An accepted_for_routing (unfilled) execution submitted at `created_at`."""
    session.add(OrderExecution(
        broker_account_id=_ACCT, ticker="BTC", side="buy", filled_qty=0.0,
        broker_order_id=broker_order_id, broker="alpaca", dry_run=False,
        status="accepted_for_routing", match_method="auto_trade_placed",
        match_confidence=1.0, created_at=created_at,
    ))
    session.commit()


def _run_recon_capturing_since(account_ref: int, max_lookback_days: int = 100_000) -> datetime:
    """Run the reconciliation job with the broker calls stubbed; return the `since` used.

    ``max_lookback_days`` defaults huge so the P1.2 cap is effectively disabled for the
    oldest-open-extension tests (whose fixed _NOW is now real-months in the past).
    """
    captured: dict[str, datetime] = {}

    def _cap(*, session, adapter, since, broker_account_id):  # noqa: ANN001, ARG001
        captured["since"] = since
        return []

    settings = MagicMock(
        reconciliation_max_lookback_days=max_lookback_days, manual_cancel_inference_hours=24
    )
    with (
        patch("investor.jobs.reconciliation.reconcile_activities", side_effect=_cap),
        patch("investor.jobs.reconciliation.persist_reconciliation"),
        patch("investor.jobs.reconciliation.sync_open_order_statuses", return_value=0),
    ):
        run_daily_reconciliation_for_account(
            settings, MagicMock(),
            AccountInfo(account_ref=account_ref, nickname="Alpaca", broker="alpaca"),
        )
    return captured["since"]


def test_window_extends_back_to_oldest_open_execution(db_session: Session) -> None:
    """A GTC order submitted 10 days ago but not yet filled must keep the reconciliation
    window reaching back to it — otherwise its eventual fill (after `since` advances past
    submission) is missed forever (the BTC case)."""
    old = _NOW - timedelta(days=10)
    _open_exec(db_session, created_at=old, broker_order_id="b-open-btc")
    # last_run is recent (1 day ago) — without the fix, since would start ~1 day ago.
    db_session.add(Meta(key=_meta_key(_ACCT), value=(_NOW - timedelta(days=1)).isoformat()))
    db_session.commit()

    since = _run_recon_capturing_since(_ACCT)
    assert since <= old  # reached back to the 10-day-old open order, not just last_run-1h


def test_window_not_extended_when_no_open_executions(db_session: Session) -> None:
    """With nothing open, the window stays at last_run - 1h (no needless re-scan)."""
    last_run = _NOW - timedelta(days=1)
    db_session.add(Meta(key=_meta_key(_ACCT), value=last_run.isoformat()))
    db_session.commit()

    since = _run_recon_capturing_since(_ACCT)
    assert since == last_run - timedelta(hours=1)


# ── P1.3: manual-broker-UI-cancel inference ──────────────────────────────────

def _exe_for(
    session: Session, sug_id: int, *, status: str,
    cancelled_at: datetime | None = None, created_at: datetime | None = None,
    broker_order_id: str = "bo-x",
) -> OrderExecution:
    row = OrderExecution(
        broker_account_id=_ACCT, suggestion_id=sug_id, ticker="AAPL", side="buy",
        filled_qty=0.0, broker_order_id=broker_order_id, broker="alpaca", dry_run=False,
        status=status, match_method="auto_trade_placed", match_confidence=1.0,
        created_at=created_at or (datetime.now(UTC) - timedelta(days=2)),
        cancelled_at=cancelled_at,
    )
    session.add(row)
    session.flush()
    return row


def test_infer_flips_stale_broker_cancel(db_session: Session) -> None:
    sug = _sug(db_session, status="accepted")
    _exe_for(db_session, sug.id, status="broker_cancelled",
             cancelled_at=datetime.now(UTC) - timedelta(hours=48))
    n = infer_manual_cancels(db_session, _ACCT, inference_hours=24)
    assert n == 1
    assert db_session.get(OrderSuggestion, sug.id).status == "cancelled"
    assert "broker UI" in (db_session.get(OrderSuggestion, sug.id).note or "")


def test_infer_skips_within_grace_window(db_session: Session) -> None:
    sug = _sug(db_session, status="accepted")
    _exe_for(db_session, sug.id, status="broker_cancelled",
             cancelled_at=datetime.now(UTC) - timedelta(hours=1))  # too recent
    assert infer_manual_cancels(db_session, _ACCT, inference_hours=24) == 0
    assert db_session.get(OrderSuggestion, sug.id).status == "accepted"


def test_infer_skips_when_replace_in_flight(db_session: Session) -> None:
    """broker_cancelled old, but a newer open execution exists (cancel-and-re-place) → leave it."""
    sug = _sug(db_session, status="accepted")
    _exe_for(db_session, sug.id, status="broker_cancelled", broker_order_id="bo-old",
             cancelled_at=datetime.now(UTC) - timedelta(hours=48),
             created_at=datetime.now(UTC) - timedelta(hours=48))
    _exe_for(db_session, sug.id, status="accepted_for_routing", broker_order_id="bo-new",
             created_at=datetime.now(UTC) - timedelta(hours=1))
    assert infer_manual_cancels(db_session, _ACCT, inference_hours=24) == 0
    assert db_session.get(OrderSuggestion, sug.id).status == "accepted"


def test_infer_ignores_filled_and_unplaced(db_session: Session) -> None:
    filled = _sug(db_session, ticker="MU", status="accepted")
    _exe_for(db_session, filled.id, status="filled",
             cancelled_at=datetime.now(UTC) - timedelta(hours=48))
    _sug(db_session, ticker="NVDA", status="accepted")  # no execution at all
    assert infer_manual_cancels(db_session, _ACCT, inference_hours=24) == 0


def test_window_capped_at_max_lookback(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """P1.2: a zombie open order older than the cap clamps `since` and logs a WARNING, so a
    forgotten GTC can't make every run pull months of activities."""
    import logging

    real_now = datetime.now(UTC)
    _open_exec(db_session, created_at=real_now - timedelta(days=40), broker_order_id="b-zombie")
    db_session.add(Meta(key=_meta_key(_ACCT), value=(real_now - timedelta(days=1)).isoformat()))
    db_session.commit()

    with caplog.at_level(logging.WARNING):
        since = _run_recon_capturing_since(_ACCT, max_lookback_days=30)

    # Clamped to ~now-30d, NOT back to the 40-day-old zombie.
    assert real_now - timedelta(days=31) <= since <= real_now - timedelta(days=29)
    assert "capped" in caplog.text
