"""Migration test for dec2737608db: add temperature + cache tiers to llm_call_log.

Upgrades to the parent (which builds the migration-managed llm_call_log), seeds one
pre-columns row, runs the migration, and asserts the new columns exist and the existing
row backfilled (temperature NULL, cache tiers 0). Then downgrades and asserts the three
columns are dropped (SQLite batch-mode drop).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

_PARENT = "fbdf8f40c65a"
_HEAD = "dec2737608db"


def _alembic_cfg(db_path: Path) -> AlembicConfig:
    cfg = AlembicConfig("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.attributes["configure_logger"] = False
    return cfg


def _seed_pre_columns_row(conn: sqlite3.Connection) -> None:
    """One llm_call_log row as it looked before this migration (no temp/cache cols)."""
    conn.execute(
        "INSERT INTO llm_call_log "
        "(ts, purpose, model, prompt_hash, input_tokens, output_tokens, cost_usd, "
        " latency_ms, status) "
        "VALUES ('2026-05-01T00:00:00+00:00', 'score_levels', 'claude-sonnet-4-6', "
        "'abc123def456', 100, 50, 0.001, 250, 'ok')"
    )
    conn.commit()


def test_upgrade_adds_columns_and_backfills(tmp_path: Path) -> None:
    db = tmp_path / "llmlog.db"
    cfg = _alembic_cfg(db)
    command.upgrade(cfg, _PARENT)

    conn = sqlite3.connect(db)
    try:
        _seed_pre_columns_row(conn)
    finally:
        conn.close()

    command.upgrade(cfg, _HEAD)

    conn = sqlite3.connect(db)
    try:
        assert (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == _HEAD
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_call_log)")}
        assert {"temperature", "cache_write_tokens", "cache_read_tokens"} <= cols

        # Existing row backfilled: temperature NULL, cache tiers 0 (server_default).
        temp, cw, cr = conn.execute(
            "SELECT temperature, cache_write_tokens, cache_read_tokens FROM llm_call_log"
        ).fetchone()
        assert temp is None
        assert cw == 0
        assert cr == 0
    finally:
        conn.close()


def test_downgrade_drops_columns(tmp_path: Path) -> None:
    db = tmp_path / "llmlog2.db"
    cfg = _alembic_cfg(db)
    command.upgrade(cfg, _HEAD)
    command.downgrade(cfg, _PARENT)

    conn = sqlite3.connect(db)
    try:
        assert (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == _PARENT
        )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_call_log)")}
        assert "temperature" not in cols
        assert "cache_write_tokens" not in cols
        assert "cache_read_tokens" not in cols
    finally:
        conn.close()
