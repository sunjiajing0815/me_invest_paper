"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.models import Base


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear broker-related env vars so each test sets only what it needs."""
    for key in [
        "BROKER", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
        "ALPACA_BASE_URL", "SQLITE_PATH", "TARGETS_PATH",
    ]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Canonical in-memory DB session: StaticPool engine + full schema, with the global
    session factory pointed at it (``override_engine_for_testing``) so code under test
    that opens its own ``session_scope()`` hits the same database.

    ``s`` / ``session`` / ``_db_session`` are aliases so migrated test files needed no
    signature churn. Files needing bespoke setup (rows seeded inside the fixture, no
    override, engine-level access) keep their own local fixture instead.
    """
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def s(db_session: Session) -> Session:
    return db_session


@pytest.fixture()
def session(db_session: Session) -> Session:
    return db_session


@pytest.fixture()
def _db_session(db_session: Session) -> Session:
    return db_session
