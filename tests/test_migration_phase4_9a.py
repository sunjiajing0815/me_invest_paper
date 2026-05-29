"""Migration test for phase4_9a multi-broker (revision d8589fe198cf).

Builds a representative *pre-4.9a* single-broker database, runs the migration,
and asserts the backfill collapses every per-account table onto Jane's one
account, seeds per-broker auto_trade_state from the old meta key, and swaps the
order_suggestion unique constraint.

Note on the schema-build strategy: `broker_account` and `positions_snapshot`
are created by SQLAlchemy ``create_all`` at app startup, *not* by any Alembic
migration (see CLAUDE.md create_all + alembic flow). So this test upgrades to
the parent revision (which builds the migration-managed tables) and then hand-
creates those two create_all-only tables in their pre-4.9a shape before running
the 4.9a migration — mirroring the real on-disk schema the migration runs against.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig

_PARENT = "62b0733b198f"
_HEAD = "d8589fe198cf"

# create_all-only tables, in their pre-4.9a shape (no broker_account_id / identity cols).
_PRE_BROKER_ACCOUNT_DDL = """
CREATE TABLE broker_account (
    id INTEGER NOT NULL PRIMARY KEY,
    account_id VARCHAR,
    broker VARCHAR NOT NULL,
    mode VARCHAR NOT NULL,
    cash_usd DOUBLE NOT NULL,
    equity_usd DOUBLE NOT NULL,
    last_sync DATETIME NOT NULL,
    effective_from DATETIME,
    effective_to DATETIME
)
"""

_PRE_POSITIONS_SNAPSHOT_DDL = """
CREATE TABLE positions_snapshot (
    id INTEGER NOT NULL PRIMARY KEY,
    account_id VARCHAR,
    ts DATETIME NOT NULL,
    ticker VARCHAR NOT NULL,
    qty DOUBLE NOT NULL,
    avg_cost DOUBLE NOT NULL,
    market_value DOUBLE NOT NULL,
    weight_pct DOUBLE NOT NULL
)
"""

_PRE_TARGET_ALLOCATION_DDL = """
CREATE TABLE target_allocation (
    id INTEGER NOT NULL PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    target_pct DOUBLE NOT NULL,
    band_low_pct DOUBLE NOT NULL,
    band_high_pct DOUBLE NOT NULL,
    effective_from DATETIME NOT NULL,
    effective_to DATETIME
)
"""


def _alembic_cfg(db_path: Path) -> AlembicConfig:
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.attributes["configure_logger"] = False
    return cfg


def _seed_pre_4_9a(conn: sqlite3.Connection) -> None:
    """Insert representative single-broker rows across the per-account tables."""
    # Two broker_account state rows: id=1 closed, id=2 the latest open row → ref 2.
    conn.executescript(
        """
        INSERT INTO broker_account
            (id, account_id, broker, mode, cash_usd, equity_usd,
             last_sync, effective_from, effective_to)
        VALUES
            (1, 'acct-x', 'alpaca', 'paper', 100.0, 1000.0,
             '2026-05-01', '2026-05-01', '2026-05-02'),
            (2, 'acct-x', 'alpaca', 'paper', 200.0, 2000.0,
             '2026-05-02', '2026-05-02', NULL);

        INSERT INTO target_allocation
            (id, ticker, target_pct, band_low_pct, band_high_pct, effective_from, effective_to)
        VALUES
            (1, 'VOO', 50.0, 40.0, 60.0, '2026-05-01', NULL),
            (2, 'QQQ', 50.0, 40.0, 60.0, '2026-05-01', NULL);

        INSERT INTO positions_snapshot
            (id, account_id, ts, ticker, qty, avg_cost, market_value, weight_pct)
        VALUES
            (1, 'acct-x', '2026-05-02', 'VOO', 3.0, 500.0, 1500.0, 75.0),
            (2, 'acct-x', '2026-05-02', 'QQQ', 1.0, 400.0, 500.0, 25.0);

        INSERT INTO order_suggestion
            (id, week_of, ticker, side, qty, limit_price, reason, status, created_at)
        VALUES
            (1, '2026-05-25', 'VOO', 'buy', 1.0, 500.0, 'test', 'pending', '2026-05-24'),
            (2, '2026-05-25', 'QQQ', 'buy', 1.0, 400.0, 'test', 'accepted', '2026-05-24');

        INSERT INTO order_execution
            (id, suggestion_id, ticker, side, filled_qty, broker,
             dry_run, status, match_method, created_at)
        VALUES
            (1, 2, 'QQQ', 'buy', 1.0, 'alpaca', 0, 'filled', 'auto_matched', '2026-05-26');

        INSERT OR REPLACE INTO meta (key, value) VALUES ('auto_trade_mode', 'LIVE');
        """
    )
    conn.commit()


@pytest.fixture()
def pre_4_9a_db(tmp_path: Path) -> Path:
    db = tmp_path / "pre_4_9a.db"
    cfg = _alembic_cfg(db)
    # 1. Build the migration-managed tables up to the parent revision.
    command.upgrade(cfg, _PARENT)
    # 2. Hand-create the create_all-only tables in their pre-4.9a shape.
    conn = sqlite3.connect(db)
    conn.executescript(_PRE_BROKER_ACCOUNT_DDL)
    conn.executescript(_PRE_POSITIONS_SNAPSHOT_DDL)
    conn.executescript(_PRE_TARGET_ALLOCATION_DDL)
    _seed_pre_4_9a(conn)
    conn.close()
    return db


def test_phase4_9a_migration_backfills_single_broker(pre_4_9a_db: Path) -> None:
    command.upgrade(_alembic_cfg(pre_4_9a_db), _HEAD)

    conn = sqlite3.connect(pre_4_9a_db)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == _HEAD

        # The canonical ref is the latest open broker_account row (id=2).
        ref = 2
        assert {r[0] for r in conn.execute("SELECT account_ref FROM broker_account")} == {ref}

        # Every per-account table: one group == ref, expected count, zero NULLs.
        for table, expected in (
            ("target_allocation", 2),
            ("positions_snapshot", 2),
            ("order_suggestion", 2),
            ("order_execution", 1),
        ):
            groups = conn.execute(
                f"SELECT broker_account_id, COUNT(*) FROM {table} GROUP BY broker_account_id"  # noqa: S608
            ).fetchall()
            assert groups == [(ref, expected)], f"{table}: {groups}"

        # Identity columns set on the latest open row.
        nickname, is_active, cfg_json = conn.execute(
            "SELECT nickname, is_active, connection_config FROM broker_account "
            "WHERE effective_to IS NULL"
        ).fetchone()
        assert nickname == "Alpaca paper"
        assert is_active == 1
        assert cfg_json is not None and "ALPACA_API_KEY" in cfg_json

        # auto_trade_state seeded from the old meta key; meta key removed.
        assert conn.execute(
            "SELECT broker_account_id, mode FROM auto_trade_state"
        ).fetchall() == [(ref, "LIVE")]
        assert conn.execute(
            "SELECT value FROM meta WHERE key='auto_trade_mode'"
        ).fetchone() is None

        # Unique constraint swapped to include broker_account_id.
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='order_suggestion' AND type='table'"
        ).fetchone()[0]
        assert "uq_suggestion_account_week" in sql
        assert "broker_account_id" in sql
    finally:
        conn.close()


def test_phase4_9a_migration_downgrade_round_trip(pre_4_9a_db: Path) -> None:
    cfg = _alembic_cfg(pre_4_9a_db)
    command.upgrade(cfg, _HEAD)
    command.downgrade(cfg, _PARENT)

    conn = sqlite3.connect(pre_4_9a_db)
    try:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == _PARENT
        # auto_trade_state dropped; meta key restored from the canonical broker.
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='auto_trade_state'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT value FROM meta WHERE key='auto_trade_mode'"
        ).fetchone() == ("LIVE",)
        # Partition column dropped.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(order_suggestion)")]
        assert "broker_account_id" not in cols
    finally:
        conn.close()
