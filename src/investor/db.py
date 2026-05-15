"""SQLite engine and session factory.

Tests call override_engine_for_testing() with an in-memory StaticPool engine.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None  # type: ignore[type-arg]


def init_db(sqlite_path: str) -> Engine:
    """Create engine, run create_all (idempotent), apply Alembic migrations, return engine."""
    global _engine, _SessionLocal
    url = f"sqlite:///{sqlite_path}"
    logger.info("Connecting to SQLite at %s", sqlite_path)
    _engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(_engine, checkfirst=True)
    alembic_cfg = AlembicConfig("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_cfg.attributes["configure_logger"] = False  # don't let alembic.ini reset our log level
    alembic_command.upgrade(alembic_cfg, "head")
    logger.info("Alembic migrations applied")
    _SessionLocal = sessionmaker(bind=_engine, autoflush=True, autocommit=False)
    logger.info("Database initialised — tables: %s", list(Base.metadata.tables.keys()))
    return _engine



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
