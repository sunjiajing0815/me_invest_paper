"""Tests for services/auto_trade.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.brokers.base import Account, BrokerValidationError, OrderConfirmation
from investor.db import override_engine_for_testing
from investor.models import (
    AutoTradeCaps,
    AutoTradeState,
    Base,
    BrokerAccount,
    KillSwitchLog,
    OrderExecution,
    OrderSuggestion,
)
from investor.services.auto_trade import (
    _check_stale_live_order,
    _GuardFailure,
    run_auto_trade_pass,
)

_NOW = datetime(2026, 5, 1, 9, 35, tzinfo=UTC)
_WEEK = date(2026, 4, 27)  # Monday
_ACCT = 1  # account_ref of the seeded primary broker account


# ── helpers ──────────────────────────────────────────────────────────────────

def _seed_account(session: Session) -> None:
    """Seed the primary broker account (account_ref=_ACCT) that resolve_primary uses."""
    session.add(
        BrokerAccount(
            account_ref=_ACCT,
            account_id="test",
            broker="alpaca",
            mode="paper",
            nickname="Test",
            is_active=True,
            cash_usd=100_000.0,
            equity_usd=100_000.0,
            last_sync=_NOW,
            effective_from=_NOW,
            effective_to=None,
        )
    )
    session.flush()


def _set_mode(session: Session, mode: str) -> None:
    state = session.get(AutoTradeState, _ACCT)
    if state is None:
        session.add(AutoTradeState(broker_account_id=_ACCT, mode=mode))
    else:
        state.mode = mode
    session.flush()


def _add_caps(
    session: Session,
    per_order: float = 500.0,
    per_day: float = 1500.0,
    per_week_ticker: float = 1000.0,
    per_day_orders: int = 5,
) -> AutoTradeCaps:
    caps = AutoTradeCaps(
        per_order_max_usd=per_order,
        per_day_max_usd=per_day,
        per_week_max_usd_per_ticker=per_week_ticker,
        per_day_max_orders=per_day_orders,
        effective_from=_NOW - timedelta(hours=1),
        effective_to=None,
    )
    session.add(caps)
    session.flush()
    return caps


def _add_suggestion(
    session: Session,
    ticker: str = "AAPL",
    side: str = "buy",
    qty: float = 3.0,
    limit_price: float = 100.0,
    status: str = "accepted",
) -> OrderSuggestion:
    sug = OrderSuggestion(
        broker_account_id=_ACCT,
        week_of=_WEEK,
        ticker=ticker,
        side=side,
        qty=qty,
        limit_price=limit_price,
        reason="test",
        status=status,
        created_at=_NOW - timedelta(hours=1),
    )
    session.add(sug)
    session.flush()
    return sug


def _mock_adapter(
    buying_power: float = 100_000.0,
    submit_order_return: OrderConfirmation | Exception | None = None,
    get_order_return: OrderConfirmation | Exception | None = None,
) -> MagicMock:
    adapter = MagicMock()
    adapter.get_account.return_value = Account(
        account_id="test",
        cash_usd=buying_power,
        equity_usd=buying_power,
        buying_power_usd=buying_power,
        as_of=_NOW,
    )
    if isinstance(submit_order_return, Exception):
        adapter.submit_order.side_effect = submit_order_return
    elif submit_order_return is not None:
        adapter.submit_order.return_value = submit_order_return

    if isinstance(get_order_return, Exception):
        adapter.get_order.side_effect = get_order_return
    elif get_order_return is not None:
        adapter.get_order.return_value = get_order_return

    return adapter


def _emailer() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as session:
        _seed_account(session)
        yield session


# ── mode = OFF ────────────────────────────────────────────────────────────────

def test_default_off_does_nothing(db_session: Session) -> None:
    _add_suggestion(db_session)
    _add_caps(db_session)
    adapter = _mock_adapter()
    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert outcomes == []
    adapter.submit_order.assert_not_called()


def test_no_meta_row_defaults_to_off(db_session: Session) -> None:
    _add_suggestion(db_session)
    _add_caps(db_session)
    outcomes = run_auto_trade_pass(
        db_session, _mock_adapter(), _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert outcomes == []


def test_off_mode_returns_empty_even_with_accepted_suggestions(db_session: Session) -> None:
    _set_mode(db_session, "OFF")
    _add_suggestion(db_session)
    _add_caps(db_session)
    outcomes = run_auto_trade_pass(
        db_session, _mock_adapter(), _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert outcomes == []


# ── DRY_RUN ───────────────────────────────────────────────────────────────────

def test_dry_run_inserts_row_no_broker_call(db_session: Session) -> None:
    _set_mode(db_session, "DRY_RUN")
    sug = _add_suggestion(db_session)
    _add_caps(db_session)
    adapter = _mock_adapter()

    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )

    assert len(outcomes) == 1
    assert outcomes[0].placed is True
    assert outcomes[0].dry_run is True
    assert outcomes[0].broker_order_id is None
    adapter.submit_order.assert_not_called()

    rows = db_session.scalars(
        select(OrderExecution).where(OrderExecution.suggestion_id == sug.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].dry_run is True
    assert rows[0].status == "dry_run"


def test_dry_run_idempotency_second_pass_skipped(db_session: Session) -> None:
    _set_mode(db_session, "DRY_RUN")
    _add_suggestion(db_session)
    _add_caps(db_session)
    adapter = _mock_adapter()

    outcomes1 = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert len(outcomes1) == 1 and outcomes1[0].placed is True

    outcomes2 = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert len(outcomes2) == 1
    assert outcomes2[0].placed is False
    assert outcomes2[0].rejected_reason is not None


# ── LIVE ──────────────────────────────────────────────────────────────────────

def test_live_end_to_end_readback_success(db_session: Session) -> None:
    _set_mode(db_session, "LIVE")
    sug = _add_suggestion(db_session)
    _add_caps(db_session)
    client_oid = f"sug-{sug.id}"
    conf = OrderConfirmation(
        broker_order_id="broker-123",
        client_order_id=client_oid,
        status="accepted",
        submitted_at=_NOW,
    )
    adapter = _mock_adapter(submit_order_return=conf, get_order_return=conf)

    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )

    assert len(outcomes) == 1
    assert outcomes[0].placed is True
    assert outcomes[0].dry_run is False
    assert outcomes[0].broker_order_id == "broker-123"

    rows = db_session.scalars(
        select(OrderExecution).where(OrderExecution.suggestion_id == sug.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].dry_run is False
    assert rows[0].status == "accepted_for_routing"


def test_live_readback_mismatch_triggers_kill_switch(db_session: Session) -> None:
    _set_mode(db_session, "LIVE")
    sug = _add_suggestion(db_session)
    _add_caps(db_session)
    client_oid = f"sug-{sug.id}"
    submit_conf = OrderConfirmation(
        broker_order_id="broker-999",
        client_order_id=client_oid,
        status="accepted",
        submitted_at=_NOW,
    )
    readback_conf = OrderConfirmation(
        broker_order_id="broker-999",
        client_order_id="sug-WRONG",  # mismatch
        status="accepted",
        submitted_at=_NOW,
    )
    adapter = _mock_adapter(submit_order_return=submit_conf, get_order_return=readback_conf)

    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )

    assert len(outcomes) == 1
    assert outcomes[0].placed is False
    assert outcomes[0].rejected_reason == "readback_mismatch"

    state = db_session.get(AutoTradeState, _ACCT)
    assert state is not None and state.mode == "OFF"

    kill_rows = db_session.scalars(select(KillSwitchLog)).all()
    assert len(kill_rows) == 1
    assert kill_rows[0].trigger == "readback_mismatch"


def test_live_broker_error_triggers_kill_switch(db_session: Session) -> None:
    _set_mode(db_session, "LIVE")
    _add_suggestion(db_session)
    _add_caps(db_session)
    adapter = _mock_adapter(submit_order_return=RuntimeError("connection refused"))

    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )

    assert len(outcomes) == 1
    assert outcomes[0].placed is False
    assert outcomes[0].rejected_reason is not None and "broker_error" in outcomes[0].rejected_reason

    state = db_session.get(AutoTradeState, _ACCT)
    assert state is not None and state.mode == "OFF"
    kill_rows = db_session.scalars(select(KillSwitchLog)).all()
    assert any(k.trigger == "broker_error" for k in kill_rows)


def test_live_idempotency_second_pass_no_second_broker_call(db_session: Session) -> None:
    _set_mode(db_session, "LIVE")
    sug = _add_suggestion(db_session)
    _add_caps(db_session)
    client_oid = f"sug-{sug.id}"
    conf = OrderConfirmation(
        broker_order_id="b-idem", client_order_id=client_oid, status="accepted", submitted_at=_NOW
    )
    adapter = _mock_adapter(submit_order_return=conf, get_order_return=conf)

    run_auto_trade_pass(db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK)
    adapter.submit_order.reset_mock()

    outcomes2 = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    adapter.submit_order.assert_not_called()
    assert all(not o.placed for o in outcomes2)


# ── Guards ────────────────────────────────────────────────────────────────────

def test_wash_sale_guard_blocks_real_buy(db_session: Session) -> None:
    _set_mode(db_session, "LIVE")
    _add_suggestion(db_session, ticker="AAPL", side="buy", qty=3.0, limit_price=100.0)
    _add_caps(db_session)

    _recent = datetime.now(UTC) - timedelta(days=5)
    recent_loss = OrderExecution(
        broker_account_id=_ACCT,
        ticker="AAPL",
        side="sell",
        filled_qty=3.0,
        filled_price=90.0,
        broker_order_id="b-loss",
        broker="alpaca",
        dry_run=False,
        status="filled",
        match_method="untracked",
        match_confidence=0.0,
        created_at=_recent,
        filled_at=_recent,
        realized_pnl_usd=-30.0,
    )
    db_session.add(recent_loss)
    db_session.flush()

    adapter = _mock_adapter()
    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert len(outcomes) == 1
    assert outcomes[0].placed is False
    assert "wash-sale" in (outcomes[0].rejected_reason or "").lower()
    adapter.submit_order.assert_not_called()


def test_wash_sale_dry_run_loss_does_not_block_real_buy(db_session: Session) -> None:
    _set_mode(db_session, "LIVE")
    sug = _add_suggestion(db_session, ticker="AAPL", side="buy", qty=3.0, limit_price=100.0)
    _add_caps(db_session)

    dry_loss = OrderExecution(
        broker_account_id=_ACCT,
        ticker="AAPL",
        side="sell",
        filled_qty=3.0,
        filled_price=90.0,
        broker_order_id=None,
        broker="dry_run",
        dry_run=True,
        status="filled",
        match_method="auto_trade_placed",
        match_confidence=1.0,
        created_at=_NOW - timedelta(days=5),
        filled_at=_NOW - timedelta(days=5),
        realized_pnl_usd=-30.0,
    )
    db_session.add(dry_loss)
    db_session.flush()

    client_oid = f"sug-{sug.id}"
    conf = OrderConfirmation(
        broker_order_id="b-wash-ok",
        client_order_id=client_oid,
        status="accepted",
        submitted_at=_NOW,
    )
    adapter = _mock_adapter(submit_order_return=conf, get_order_return=conf)
    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert len(outcomes) == 1
    assert outcomes[0].placed is True


def test_no_active_caps_skips_suggestion_not_kill_switch(db_session: Session) -> None:
    """No active caps row → guard failure (skip suggestion), kill switch NOT fired."""
    _set_mode(db_session, "LIVE")
    _add_suggestion(db_session)
    # No caps added

    adapter = _mock_adapter()
    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert len(outcomes) == 1
    assert outcomes[0].placed is False
    assert outcomes[0].rejected_reason is not None
    adapter.submit_order.assert_not_called()

    state = db_session.get(AutoTradeState, _ACCT)
    assert state is not None and state.mode == "LIVE"

    kill_rows = db_session.scalars(select(KillSwitchLog)).all()
    assert kill_rows == []


def test_per_order_cap_blocks_suggestion(db_session: Session) -> None:
    _set_mode(db_session, "LIVE")
    _add_suggestion(db_session, qty=3.0, limit_price=200.0)  # cost = 600 > cap
    _add_caps(db_session, per_order=500.0)

    adapter = _mock_adapter()
    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert len(outcomes) == 1
    assert outcomes[0].placed is False
    assert "per-order cap" in (outcomes[0].rejected_reason or "")
    adapter.submit_order.assert_not_called()


def test_only_accepted_suggestions_processed(db_session: Session) -> None:
    _set_mode(db_session, "LIVE")
    _add_suggestion(db_session, status="pending")
    _add_suggestion(db_session, ticker="MSFT", status="rejected")
    _add_caps(db_session)

    outcomes = run_auto_trade_pass(
        db_session, _mock_adapter(), _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert outcomes == []


def test_live_readback_exception_triggers_kill_switch(db_session: Session) -> None:
    """get_order() raising an exception triggers kill switch (readback_failed)."""
    _set_mode(db_session, "LIVE")
    sug = _add_suggestion(db_session)
    _add_caps(db_session)
    client_oid = f"sug-{sug.id}"
    conf = OrderConfirmation(
        broker_order_id="broker-eof",
        client_order_id=client_oid,
        status="accepted",
        submitted_at=_NOW,
    )
    adapter = _mock_adapter(
        submit_order_return=conf,
        get_order_return=RuntimeError("timeout reading response"),
    )
    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert len(outcomes) == 1
    assert outcomes[0].placed is False
    assert "readback_failed" in (outcomes[0].rejected_reason or "")
    state = db_session.get(AutoTradeState, _ACCT)
    assert state is not None and state.mode == "OFF"
    kill_rows = db_session.scalars(select(KillSwitchLog)).all()
    assert any(k.trigger == "readback_failed" for k in kill_rows)


def test_dry_run_multiple_suggestions_all_inserted(db_session: Session) -> None:
    """All accepted suggestions get a dry_run row in one pass."""
    _set_mode(db_session, "DRY_RUN")
    _add_suggestion(db_session, ticker="AAPL")
    _add_suggestion(db_session, ticker="MSFT")
    _add_caps(db_session)

    outcomes = run_auto_trade_pass(
        db_session, _mock_adapter(), _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )
    assert len(outcomes) == 2
    assert all(o.placed for o in outcomes)
    rows = db_session.scalars(
        select(OrderExecution).where(OrderExecution.dry_run.is_(True))
    ).all()
    assert len(rows) == 2


# ── BrokerValidationError ─────────────────────────────────────────────────────

def test_validation_error_skips_suggestion_stays_live(db_session: Session) -> None:
    """BrokerValidationError on sug-1: sug-1 skipped, sug-2 processed, no kill switch."""
    _set_mode(db_session, "LIVE")
    sug1 = _add_suggestion(db_session, ticker="AAPL")
    sug2 = _add_suggestion(db_session, ticker="MSFT")
    _add_caps(db_session)

    client_oid2 = f"sug-{sug2.id}"
    conf2 = OrderConfirmation(
        broker_order_id="broker-msft",
        client_order_id=client_oid2,
        status="accepted",
        submitted_at=_NOW,
    )
    # First call raises BrokerValidationError; second call succeeds
    adapter = MagicMock()
    adapter.get_account.return_value = Account(
        account_id="test",
        cash_usd=100_000.0,
        equity_usd=100_000.0,
        buying_power_usd=100_000.0,
        as_of=_NOW,
    )
    adapter.submit_order.side_effect = [
        BrokerValidationError("invalid limit_price: sub-penny"),
        conf2,
    ]
    adapter.get_order.return_value = conf2

    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )

    assert len(outcomes) == 2
    # sug-1 skipped with validation reason
    assert outcomes[0].suggestion_id == sug1.id
    assert outcomes[0].placed is False
    assert "broker_validation" in (outcomes[0].rejected_reason or "")
    # sug-2 successfully placed
    assert outcomes[1].suggestion_id == sug2.id
    assert outcomes[1].placed is True
    assert outcomes[1].broker_order_id == "broker-msft"

    # Kill switch must NOT have fired — mode stays LIVE
    state = db_session.get(AutoTradeState, _ACCT)
    assert state is not None and state.mode == "LIVE"
    kill_rows = db_session.scalars(select(KillSwitchLog)).all()
    assert kill_rows == []


def test_real_broker_error_fires_kill_switch(db_session: Session) -> None:
    """Non-validation exception fires kill switch and stops processing."""
    _set_mode(db_session, "LIVE")
    _add_suggestion(db_session, ticker="AAPL")
    _add_suggestion(db_session, ticker="MSFT")
    _add_caps(db_session)

    adapter = _mock_adapter(submit_order_return=RuntimeError("connection refused"))

    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )

    # Only one outcome — processing stopped after the first failure
    assert len(outcomes) == 1
    assert outcomes[0].placed is False
    assert "broker_error" in (outcomes[0].rejected_reason or "")

    # Kill switch fired — mode is OFF
    state = db_session.get(AutoTradeState, _ACCT)
    assert state is not None and state.mode == "OFF"
    kill_rows = db_session.scalars(select(KillSwitchLog)).all()
    assert any(k.trigger == "broker_error" for k in kill_rows)


def _add_stale_exec(db_session: Session, sug_id: int, ticker: str, order_id: str) -> OrderExecution:
    """A prior-week accepted_for_routing real execution — the kind the guard reconciles."""
    ex = OrderExecution(
        broker_account_id=_ACCT, suggestion_id=sug_id, ticker=ticker, side="buy",
        submitted_qty=2.0, filled_qty=0, limit_price=98.0, broker="alpaca",
        broker_order_id=order_id, client_order_id=f"sug-{sug_id}", dry_run=False,
        status="accepted_for_routing", match_method="auto_trade_placed",
        match_confidence=1.0, created_at=_NOW - timedelta(days=7),
    )
    db_session.add(ex)
    db_session.flush()
    return ex


def _conf(order_id: str, status: str) -> OrderConfirmation:
    return OrderConfirmation(
        broker_order_id=order_id, client_order_id=None, status=status, submitted_at=_NOW
    )


def test_stale_open_order_cancelled_and_row_cleared(db_session: Session) -> None:
    """LIVE: a prior order still OPEN at the broker is cancelled (cancel-and-replace) and
    its stale row cleared, so the guard no longer blocks this week's suggestion."""
    cur = _add_suggestion(db_session, ticker="AAPL", side="buy", qty=2.0, limit_price=100.0)
    stale = _add_stale_exec(db_session, cur.id + 1, "AAPL", "stale-open")
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("stale-open", "new")  # still working at broker
    _check_stale_live_order(db_session, adapter, cur, _ACCT, "LIVE")  # must NOT raise
    adapter.cancel_order.assert_called_once_with("stale-open")
    assert stale.status == "broker_cancelled"


def test_stale_filled_order_cleared_without_cancel(db_session: Session) -> None:
    """LIVE: a prior order already FILLED at the broker (the GOOG bug — sync only marks
    cancellations, not fills) leaves a stale row; clear it without cancelling, don't block."""
    cur = _add_suggestion(db_session, ticker="GOOG", side="buy", qty=2.0, limit_price=100.0)
    stale = _add_stale_exec(db_session, cur.id + 1, "GOOG", "stale-filled")
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("stale-filled", "filled")
    _check_stale_live_order(db_session, adapter, cur, _ACCT, "LIVE")  # must NOT raise
    adapter.cancel_order.assert_not_called()
    assert stale.status == "broker_cancelled"


def test_stale_order_blocks_when_broker_status_unknown(db_session: Session) -> None:
    """LIVE: if the broker status can't be fetched, skip (block) rather than risk a dup."""
    cur = _add_suggestion(db_session, ticker="MSFT", side="buy", qty=2.0, limit_price=100.0)
    stale = _add_stale_exec(db_session, cur.id + 1, "MSFT", "stale-unknown")
    adapter = MagicMock()
    adapter.get_order.side_effect = RuntimeError("broker down")
    with pytest.raises(_GuardFailure):
        _check_stale_live_order(db_session, adapter, cur, _ACCT, "LIVE")
    adapter.cancel_order.assert_not_called()
    assert stale.status == "accepted_for_routing"  # unchanged — still blocks


def test_stale_order_dry_run_blocks_without_broker_calls(db_session: Session) -> None:
    """DRY_RUN must never touch the broker — keep the conservative block."""
    cur = _add_suggestion(db_session, ticker="VOO", side="buy", qty=2.0, limit_price=100.0)
    _add_stale_exec(db_session, cur.id + 1, "VOO", "stale-dry")
    adapter = MagicMock()
    with pytest.raises(_GuardFailure):
        _check_stale_live_order(db_session, adapter, cur, _ACCT, "DRY_RUN")
    adapter.get_order.assert_not_called()
    adapter.cancel_order.assert_not_called()


# ── per-broker mode isolation (Phase 4.9a B1) ────────────────────────────────

def test_mode_is_per_broker_account(db_session: Session) -> None:
    """auto_trade_state mode is per broker account — account A LIVE does not enable account B."""
    acct_b = 2
    db_session.add(
        BrokerAccount(
            account_ref=acct_b,
            account_id="test-b",
            broker="moomoo",
            mode="live",
            nickname="Broker B",
            is_active=True,
            cash_usd=100_000.0,
            equity_usd=100_000.0,
            last_sync=_NOW,
            effective_from=_NOW,
            effective_to=None,
        )
    )
    db_session.flush()

    _set_mode(db_session, "LIVE")  # account A (_ACCT) → LIVE
    db_session.add(AutoTradeState(broker_account_id=acct_b, mode="OFF"))  # account B → OFF
    _add_caps(db_session)

    # A suggestion on account B should NOT trade (B is OFF), even though A is LIVE.
    sug_b = OrderSuggestion(
        broker_account_id=acct_b,
        week_of=_WEEK,
        ticker="TSLA",
        side="buy",
        qty=1.0,
        limit_price=100.0,
        reason="test",
        status="accepted",
        created_at=_NOW - timedelta(hours=1),
    )
    db_session.add(sug_b)
    db_session.flush()

    adapter = _mock_adapter()
    outcomes_b = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "moomoo",
        broker_account_id=acct_b, as_of=_WEEK,
    )
    assert outcomes_b == []  # B is OFF
    adapter.submit_order.assert_not_called()
