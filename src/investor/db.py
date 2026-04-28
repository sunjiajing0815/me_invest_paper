"""DuckDB engine and session factory.

Single-writer constraint: only one process opens the file-based engine.
Use pool_size=1 to avoid DuckDB lock contention.
Tests call override_engine_for_testing() with an in-memory StaticPool engine.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None  # type: ignore[type-arg]


def init_db(duckdb_path: str) -> Engine:
    """Create engine, run create_all (idempotent), return engine."""
    global _engine, _SessionLocal
    url = f"duckdb:///{duckdb_path}"
    logger.info("Connecting to DuckDB at %s", duckdb_path)
    _engine = create_engine(url, pool_size=1, future=True)
    Base.metadata.create_all(_engine, checkfirst=True)
    _migrate_broker_account_columns(_engine)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=True, autocommit=False)
    logger.info("Database initialised — tables: %s", list(Base.metadata.tables.keys()))
    return _engine


def _migrate_broker_account_columns(engine: Engine) -> None:
    """Add columns introduced after initial schema to existing DB files."""
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE broker_account ADD COLUMN IF NOT EXISTS effective_from TIMESTAMPTZ"
        ))
        conn.execute(text(
            "ALTER TABLE broker_account ADD COLUMN IF NOT EXISTS effective_to TIMESTAMPTZ"
        ))
        conn.execute(text(
            "ALTER TABLE broker_account ADD COLUMN IF NOT EXISTS account_id VARCHAR"
        ))
        conn.execute(text(
            "ALTER TABLE positions_snapshot ADD COLUMN IF NOT EXISTS account_id VARCHAR"
        ))
        conn.commit()


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine


def get_session_factory() -> sessionmaker[Session]:  # type: ignore[type-arg]
    if _SessionLocal is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _SessionLocal


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager: provides a transactional session, commits or rolls back."""
    factory = get_session_factory()
    sess = factory()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def override_engine_for_testing(engine: Engine) -> None:
    """Replace module-level engine and session factory (tests only)."""
    global _engine, _SessionLocal
    _engine = engine
    Base.metadata.create_all(engine, checkfirst=True)
    _SessionLocal = sessionmaker(bind=engine, autoflush=True, autocommit=False)
