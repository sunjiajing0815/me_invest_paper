"""Tests for src/investor/jobs/suggestion_expiry.py."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.jobs.suggestion_expiry import sweep_expired_suggestions
from investor.models import Base, OrderExecution, OrderSuggestion

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _make_session_factory(session: Session):  # type: ignore[return]
    """Return a contextmanager-based session factory that reuses an existing session."""

    @contextmanager
    def factory() -> Generator[Session, None, None]:
        yield session

    return factory


def _seed_suggestion(
    session: Session,
    *,
    status: str = "pending",
    expires_at: datetime,
    ticker: str = "AAPL",
) -> OrderSuggestion:
    row = OrderSuggestion(
        week_of=date(2026, 5, 19),
        ticker=ticker,
        side="buy",
        qty=2.0,
        limit_price=150.0,
        reason="test",
        status=status,
        expires_at=expires_at,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def _seed_execution(
    session: Session,
    *,
    suggestion_id: int,
    broker_order_id: str | None = "ord-abc",
    dry_run: bool = False,
) -> OrderExecution:
    row = OrderExecution(
        suggestion_id=suggestion_id,
        ticker="AAPL",
        side="buy",
        submitted_qty=2.0,
        filled_qty=0.0,
        limit_price=150.0,
        broker=  "alpaca",
        broker_order_id=broker_order_id,
        client_order_id=f"sug-{suggestion_id}",
        dry_run=dry_run,
        status="accepted_for_routing",
        match_method="auto_trade_placed",
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSweepExpiredSuggestions:
    def test_sweep_expires_stale_pending(self, db_session: Session) -> None:
        """Pending row with past expires_at → status becomes 'expired'."""
        past = datetime.now(UTC) - timedelta(days=2)
        row = _seed_suggestion(db_session, status="pending", expires_at=past)
        db_session.commit()

        sweep_expired_suggestions(session_factory=_make_session_factory(db_session))

        db_session.refresh(row)
        assert row.status == "expired"
        assert row.acted_at is not None

    def test_sweep_ignores_future_expiry(self, db_session: Session) -> None:
        """Pending row with future expires_at stays pending after sweep."""
        future = datetime.now(UTC) + timedelta(days=7)
        row = _seed_suggestion(db_session, status="pending", expires_at=future)
        db_session.commit()

        sweep_expired_suggestions(session_factory=_make_session_factory(db_session))

        db_session.refresh(row)
        assert row.status == "pending"

    def test_sweep_expires_stale_accepted_no_execution(self, db_session: Session) -> None:
        """Accepted row with past expires_at and no execution row → marked expired."""
        past = datetime.now(UTC) - timedelta(days=2)
        row = _seed_suggestion(db_session, status="accepted", expires_at=past)
        db_session.commit()

        adapter = MagicMock()
        sweep_expired_suggestions(
            adapter=adapter, session_factory=_make_session_factory(db_session)
        )

        db_session.refresh(row)
        assert row.status == "expired"
        adapter.cancel_order.assert_not_called()

    def test_sweep_cancels_broker_order_for_accepted(self, db_session: Session) -> None:
        """Accepted row with linked OrderExecution → cancel_order called, row expired."""
        past = datetime.now(UTC) - timedelta(days=2)
        sug = _seed_suggestion(db_session, status="accepted", expires_at=past)
        _seed_execution(db_session, suggestion_id=sug.id, broker_order_id="ord-xyz")
        db_session.commit()

        adapter = MagicMock()
        sweep_expired_suggestions(
            adapter=adapter, session_factory=_make_session_factory(db_session)
        )

        adapter.cancel_order.assert_called_once_with("ord-xyz")
        db_session.refresh(sug)
        assert sug.status == "expired"
        assert sug.acted_at is not None

    def test_sweep_skips_dry_run_execution(self, db_session: Session) -> None:
        """DRY_RUN execution rows are ignored — no cancel_order call."""
        past = datetime.now(UTC) - timedelta(days=2)
        sug = _seed_suggestion(db_session, status="accepted", expires_at=past)
        _seed_execution(db_session, suggestion_id=sug.id, broker_order_id="ord-dry", dry_run=True)
        db_session.commit()

        adapter = MagicMock()
        sweep_expired_suggestions(
            adapter=adapter, session_factory=_make_session_factory(db_session)
        )

        adapter.cancel_order.assert_not_called()
        db_session.refresh(sug)
        assert sug.status == "expired"

    def test_sweep_cancel_failure_still_expires(self, db_session: Session) -> None:
        """If cancel_order raises, the suggestion is still marked expired."""
        past = datetime.now(UTC) - timedelta(days=2)
        sug = _seed_suggestion(db_session, status="accepted", expires_at=past)
        _seed_execution(db_session, suggestion_id=sug.id, broker_order_id="ord-fail")
        db_session.commit()

        adapter = MagicMock()
        adapter.cancel_order.side_effect = RuntimeError("broker down")
        sweep_expired_suggestions(
            adapter=adapter, session_factory=_make_session_factory(db_session)
        )

        db_session.refresh(sug)
        assert sug.status == "expired"

    def test_sweep_no_adapter_skips_cancel(self, db_session: Session) -> None:
        """Without an adapter, accepted rows expire without attempting cancel."""
        past = datetime.now(UTC) - timedelta(days=2)
        sug = _seed_suggestion(db_session, status="accepted", expires_at=past)
        _seed_execution(db_session, suggestion_id=sug.id, broker_order_id="ord-abc")
        db_session.commit()

        # No adapter passed — should not raise
        sweep_expired_suggestions(session_factory=_make_session_factory(db_session))

        db_session.refresh(sug)
        assert sug.status == "expired"

    def test_sweep_updates_exec_status_to_broker_cancelled_after_cancel(
        self, db_session: Session
    ) -> None:
        """cancel_order + get_order non-filled → exec status becomes 'broker_cancelled'."""
        past = datetime.now(UTC) - timedelta(minutes=5)
        sug = _seed_suggestion(db_session, status="accepted", expires_at=past)
        exec_row = _seed_execution(db_session, suggestion_id=sug.id, broker_order_id="ord-abc")
        db_session.commit()

        adapter = MagicMock()
        adapter.get_order.return_value = MagicMock(status="canceled")
        sweep_expired_suggestions(
            adapter=adapter, session_factory=_make_session_factory(db_session)
        )

        db_session.refresh(exec_row)
        assert exec_row.status == "broker_cancelled"

    def test_sweep_cancel_failure_leaves_exec_status_unchanged(
        self, db_session: Session
    ) -> None:
        """If cancel_order raises, the execution row status stays 'accepted_for_routing'."""
        past = datetime.now(UTC) - timedelta(minutes=5)
        sug = _seed_suggestion(db_session, status="accepted", expires_at=past)
        exec_row = _seed_execution(db_session, suggestion_id=sug.id, broker_order_id="ord-abc")
        db_session.commit()

        adapter = MagicMock()
        adapter.cancel_order.side_effect = Exception("broker down")
        sweep_expired_suggestions(
            adapter=adapter, session_factory=_make_session_factory(db_session)
        )

        db_session.refresh(exec_row)
        assert exec_row.status == "accepted_for_routing"

    def test_sweep_does_not_set_broker_cancelled_when_order_already_filled(
        self, db_session: Session
    ) -> None:
        """cancel_order succeeds but get_order returns filled → exec unchanged; sug expired."""
        past = datetime.now(UTC) - timedelta(minutes=5)
        sug = _seed_suggestion(db_session, status="accepted", expires_at=past)
        exec_row = _seed_execution(db_session, suggestion_id=sug.id, broker_order_id="ord-filled")
        db_session.commit()

        adapter = MagicMock()
        adapter.get_order.return_value = MagicMock(status="filled")
        sweep_expired_suggestions(
            adapter=adapter, session_factory=_make_session_factory(db_session)
        )

        db_session.refresh(exec_row)
        assert exec_row.status == "accepted_for_routing"  # NOT broker_cancelled

        db_session.refresh(sug)
        assert sug.status == "expired"  # suggestion is still expired
