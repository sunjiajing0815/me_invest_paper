"""Tests for UTCDateTime TypeDecorator."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.investor.models import Base, WeeklyMarketContextRow


@pytest.fixture()
def mem_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as sess:
        yield sess


def test_utcdatetime_reads_back_tz_aware(mem_session: Session) -> None:
    """Datetime inserted as UTC is read back with tzinfo intact."""
    now = datetime.now(UTC)
    row = WeeklyMarketContextRow(week_of=now.date(), payload_json="{}", created_at=now)
    mem_session.add(row)
    mem_session.flush()
    mem_session.expire(row)
    fetched = mem_session.get(WeeklyMarketContextRow, row.id)
    assert fetched is not None
    assert fetched.created_at.tzinfo is not None
