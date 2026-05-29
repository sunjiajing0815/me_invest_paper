"""Regression test: the Alembic chain builds the complete schema on its own.

Locks in three properties that were broken before the foundation-hardening work:
- `f2680` no longer crashes on a fresh DB (the `checkpoints`/`writes` DROPs are
  guarded with IF EXISTS),
- the `adopt_legacy_create_all_tables` migration brings the former create_all-only
  tables (broker_account, target_allocation, positions_snapshot) under Alembic, and
- therefore a fresh `alembic upgrade head` yields *every* model table — Alembic is
  the single source of truth, `create_all` is only a backstop in `init_db`.

If a future model adds a table without a migration, this test fails.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

from investor.models import Base


def test_fresh_alembic_upgrade_builds_every_model_table(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    cfg.attributes["configure_logger"] = False

    # No create_all first — Alembic alone must build the whole schema.
    command.upgrade(cfg, "head")

    import sqlite3

    conn = sqlite3.connect(db)
    try:
        present = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()

    missing = set(Base.metadata.tables) - present
    assert not missing, f"migration chain is missing model tables: {sorted(missing)}"
