"""Tests for services/backup.py — VACUUM INTO snapshot + retention."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from investor.services.backup import backup_database


def _make_db(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def test_backup_produces_openable_copy_with_rows(tmp_path: Path) -> None:
    src = tmp_path / "investor.db"
    _make_db(src, rows=5)
    dest = backup_database(str(src), str(tmp_path / "backups"), keep=8)

    assert dest.exists() and dest.parent.name == "backups"
    conn = sqlite3.connect(str(dest))
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5
    finally:
        conn.close()


def test_backup_creates_missing_dir(tmp_path: Path) -> None:
    src = tmp_path / "investor.db"
    _make_db(src)
    out = tmp_path / "nested" / "backups"
    assert not out.exists()
    dest = backup_database(str(src), str(out), keep=8)
    assert dest.exists() and out.is_dir()


def test_retention_keeps_newest_n(tmp_path: Path) -> None:
    src = tmp_path / "investor.db"
    _make_db(src)
    out = tmp_path / "backups"
    out.mkdir()
    # Seed 5 older backups with sortable (older) timestamped names.
    for i in range(5):
        (out / f"investor-2026010{i}T000000Z.db").write_bytes(b"old")
    backup_database(str(src), str(out), keep=3)  # +1 real → prune to newest 3
    remaining = sorted(p.name for p in out.glob("investor-*.db"))
    assert len(remaining) == 3
    # The just-written backup (today's UTC stamp) sorts last → must survive.
    assert remaining[-1].startswith("investor-") and remaining[-1] != "investor-20260104T000000Z.db"


def test_missing_source_raises(tmp_path: Path) -> None:
    import pytest
    with pytest.raises(FileNotFoundError):
        backup_database(str(tmp_path / "nope.db"), str(tmp_path / "backups"))


def test_keep_zero_prunes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "investor.db"
    _make_db(src)
    out = tmp_path / "backups"
    out.mkdir()
    (out / "investor-20260101T000000Z.db").write_bytes(b"old")
    backup_database(str(src), str(out), keep=0)
    assert len(list(out.glob("investor-*.db"))) == 2  # nothing pruned
