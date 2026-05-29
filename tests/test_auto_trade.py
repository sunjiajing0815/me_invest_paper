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
    Base,
    KillSwitchLog,
    Meta,
    OrderExecution,
    OrderSuggestion,
)
from investor.services.auto_trade import run_auto_trade_pass

_NOW = datetime(2026, 5, 1, 9, 35, tzinfo=UTC)
_WEEK = date(2026, 4, 27)  # Monday


# ── helpers ──────────────────────────────────────────────────────────────────

def _set_mode(session: Session, mode: str) -> None:
    meta = session.get(Meta, "auto_trade_mode")
    if meta is None:
        session.add(Meta(key="auto_trade_mode", value=mode))
    else:
        meta.value = mode
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

    meta = db_session.get(Meta, "auto_trade_mode")
    assert meta is not None and meta.value == "OFF"

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

    meta = db_session.get(Meta, "auto_trade_mode")
    assert meta is not None and meta.value == "OFF"
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

    meta = db_session.get(Meta, "auto_trade_mode")
    assert meta is not None and meta.value == "LIVE"

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
    meta = db_session.get(Meta, "auto_trade_mode")
    assert meta is not None and meta.value == "OFF"
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
    meta = db_session.get(Meta, "auto_trade_mode")
    assert meta is not None and meta.value == "LIVE"
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
    meta = db_session.get(Meta, "auto_trade_mode")
    assert meta is not None and meta.value == "OFF"
    kill_rows = db_session.scalars(select(KillSwitchLog)).all()
    assert any(k.trigger == "broker_error" for k in kill_rows)


def test_stale_live_order_blocks_placement(db_session: Session) -> None:
    """_check_stale_live_order blocks placement when another sug has accepted_for_routing exec."""
    _set_mode(db_session, "LIVE")
    _add_caps(
        db_session,
        per_order=10_000.0,
        per_day=50_000.0,
        per_week_ticker=50_000.0,
        per_day_orders=20,
    )

    # This week's suggestion for AAPL — the one auto_trade would try to place
    current_sug = _add_suggestion(db_session, ticker="AAPL", side="buy", qty=2.0, limit_price=100.0)

    # A different (older) suggestion for AAPL — simulates a GTC from last week
    old_sug = OrderSuggestion(
        week_of=_WEEK - timedelta(days=7),
        ticker="AAPL",
        side="buy",
        qty=2.0,
        limit_price=98.0,
        reason="last week suggestion",
        status="accepted",
        created_at=_NOW - timedelta(days=7),
    )
    db_session.add(old_sug)
    db_session.flush()

    # Linked accepted_for_routing execution for the old suggestion (dry_run=False)
    stale_exec = OrderExecution(
        suggestion_id=old_sug.id,
        ticker="AAPL",
        side="buy",
        submitted_qty=2.0,
        filled_qty=0,
        limit_price=98.0,
        broker="alpaca",
        broker_order_id="stale-ord-001",
        client_order_id=f"sug-{old_sug.id}",
        dry_run=False,
        status="accepted_for_routing",
        match_method="auto_trade_placed",
        match_confidence=1.0,
        created_at=_NOW - timedelta(days=7),
    )
    db_session.add(stale_exec)
    db_session.flush()

    adapter = _mock_adapter()
    outcomes = run_auto_trade_pass(
        db_session, adapter, _emailer(), "t@t.com", "alpaca", as_of=_WEEK
    )

    assert len(outcomes) == 1
    assert outcomes[0].suggestion_id == current_sug.id
    assert outcomes[0].placed is False
    assert "stale live order" in (outcomes[0].rejected_reason or "")
    adapter.submit_order.assert_not_called()
