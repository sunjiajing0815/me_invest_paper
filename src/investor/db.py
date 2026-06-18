"""SQLite engine and session factory.

Tests call override_engine_for_testing() with an in-memory StaticPool engine.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """Force a bind-mount-safe, durable journaling config on every connection.

    WAL mode is unsafe on Docker Desktop bind mounts (and any network-like
    filesystem): it relies on a memory-mapped ``-shm`` file + POSIX locking the
    virtualised mount doesn't honour, so committed transactions can sit in the
    ``-wal`` file and be lost on the next incoherent checkpoint/restart (this is
    exactly how a target reload was silently rolled back — see ADR-0026). This app
    is single-writer (CLAUDE.md convention #7), so WAL buys nothing; we use the
    rollback journal (``DELETE``) which needs no ``-shm``/mmap, plus ``synchronous=FULL``
    for maximal durability. ``journal_mode`` is persistent (converts the file on
    first connect); ``synchronous`` is per-connection, so we set both on every connect.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=DELETE")
            cur.execute("PRAGMA synchronous=FULL")
        finally:
            cur.close()


def init_db(sqlite_path: str) -> Engine:
    """Create engine, apply Alembic migrations, then create_all as a backstop; return engine.

    Alembic is the single source of truth for the schema — it runs FIRST and builds
    the complete schema from the migration chain. ``create_all(checkfirst=True)`` runs
    AFTER only as a safety net for any model table not yet covered by a migration.
    (Order matters: the reverse — create_all then alembic — collides on a fresh DB,
    because create_all builds every current-model table and the create_table migrations
    then hit "already exists.") New tables MUST get a migration; create_all is not a
    substitute. Tests build the schema via ``override_engine_for_testing`` (create_all).
    """
    global _engine, _SessionLocal
    url = f"sqlite:///{sqlite_path}"
    logger.info("Connecting to SQLite at %s", sqlite_path)
    _engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)
    _apply_sqlite_pragmas(_engine)  # convert off WAL before any connection is opened
    # Fail fast if the DB is still in WAL mode (the bind-mount data-loss footgun).
    with _engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    logger.info("SQLite journal_mode=%s, synchronous=FULL", mode)
    if mode == "wal":
        raise RuntimeError(
            f"SQLite at {sqlite_path} is in WAL mode; expected DELETE. WAL is unsafe on "
            "bind mounts and has caused silent data loss (ADR-0026)."
        )
    alembic_cfg = AlembicConfig("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_cfg.attributes["configure_logger"] = False  # don't let alembic.ini reset our log level
    alembic_command.upgrade(alembic_cfg, "head")
    logger.info("Alembic migrations applied")
    Base.metadata.create_all(_engine, checkfirst=True)  # backstop only — alembic owns the schema
    _SessionLocal = sessionmaker(bind=_engine, autoflush=True, autocommit=False)
    logger.info("Database initialised — tables: %s", list(Base.metadata.tables.keys()))
    return _engine



def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _engine


def get_session_factory() -> sessionmaker[Session]:
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
