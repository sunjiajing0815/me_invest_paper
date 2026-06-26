"""SQLite backup via ``VACUUM INTO`` (soak-window P0.3).

The OLTP DB lives on the ``me_invest_dbdata`` named volume (ADR-0026), which host backup
tooling (Time Machine) no longer covers. This writes a consistent snapshot into the
bind-mounted ``data/backups/`` directory — which IS host-visible/Time-Machined — and prunes to
the newest ``keep`` copies. ``VACUUM INTO`` produces a transactionally-consistent copy even with
a concurrent reader, so it is safe to run against the live DB.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PREFIX = "investor-"
_SUFFIX = ".db"


def backup_database(sqlite_path: str, backups_dir: str, keep: int = 8) -> Path:
    """Write a consistent snapshot of ``sqlite_path`` into ``backups_dir`` and prune to ``keep``.

    Returns the path of the new backup. Raises ``FileNotFoundError`` if the source DB is absent.
    """
    src = Path(sqlite_path)
    if not src.exists():
        raise FileNotFoundError(f"source DB not found: {src}")

    out_dir = Path(backups_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = out_dir / f"{_PREFIX}{stamp}{_SUFFIX}"

    conn = sqlite3.connect(str(src))
    try:
        # VACUUM INTO needs a literal-or-expression target; bind as a parameter (SQLite >= 3.27).
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()

    _prune(out_dir, keep)
    logger.info("DB backup written: %s (%d bytes)", dest, dest.stat().st_size)
    return dest


def _prune(out_dir: Path, keep: int) -> None:
    """Delete all but the newest ``keep`` ``investor-*.db`` files (names sort oldest-first)."""
    if keep <= 0:
        return
    backups = sorted(out_dir.glob(f"{_PREFIX}*{_SUFFIX}"))
    for old in backups[:-keep]:
        old.unlink()
        logger.info("pruned old backup: %s", old.name)
