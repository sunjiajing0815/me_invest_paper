"""Tests for src/investor/jobs/suggestion_expiry.py."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.jobs.suggestion_expiry import sweep_expired_suggestions
from investor.models import Base, OrderSuggestion

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
) -> OrderSuggestion:
    from datetime import date

    row = OrderSuggestion(
        week_of=date(2026, 5, 19),
        ticker="AAPL",
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSweepExpiredSuggestions:
    def test_sweep_expires_stale_pending(self, db_session: Session) -> None:
        """Smoke row 13: pending row with past expires_at → status becomes 'expired'."""
        past_expires = datetime.now(UTC) - timedelta(days=2)
        row = _seed_suggestion(db_session, status="pending", expires_at=past_expires)
        db_session.commit()

        factory = _make_session_factory(db_session)
        sweep_expired_suggestions(session_factory=factory)

        db_session.refresh(row)
        assert row.status == "expired"
        assert row.acted_at is not None

    def test_sweep_ignores_future_expiry(self, db_session: Session) -> None:
        """Pending row with future expires_at stays pending after sweep."""
        future_expires = datetime.now(UTC) + timedelta(days=7)
        row = _seed_suggestion(db_session, status="pending", expires_at=future_expires)
        db_session.commit()

        factory = _make_session_factory(db_session)
        sweep_expired_suggestions(session_factory=factory)

        db_session.refresh(row)
        assert row.status == "pending"

    def test_sweep_ignores_non_pending(self, db_session: Session) -> None:
        """An 'accepted' row with past expires_at stays 'accepted' after sweep."""
        past_expires = datetime.now(UTC) - timedelta(days=2)
        row = _seed_suggestion(db_session, status="accepted", expires_at=past_expires)
        db_session.commit()

        factory = _make_session_factory(db_session)
        sweep_expired_suggestions(session_factory=factory)

        db_session.refresh(row)
        assert row.status == "accepted"
