"""Tests for UTCDateTime TypeDecorator + SQLite journaling pragmas."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.investor.db import _apply_sqlite_pragmas
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


def test_pragmas_force_delete_journal_not_wal(tmp_path: Path) -> None:
    """A file engine with our pragma listener never stays in WAL mode."""
    db = tmp_path / "x.db"
    # Pre-seed the file into WAL mode (persistent on disk), then dispose so no
    # pooled connection survives — mirrors a pre-existing WAL DB on disk.
    seed = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    with seed.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() == "wal"
    seed.dispose()

    # A fresh engine with the listener attached *before* any connection converts it.
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    _apply_sqlite_pragmas(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "delete"
        assert c.exec_driver_sql("PRAGMA synchronous").scalar() == 2  # FULL
    engine.dispose()
