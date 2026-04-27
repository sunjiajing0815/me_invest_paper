"""SQL query registry. All queries live in src/investor/sql/*.sql.

Import named constants from here rather than writing SQL inline in Python.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import TextClause, text

_SQL_DIR = Path(__file__).parent / "sql"


def _q(filename: str) -> TextClause:
    return text((_SQL_DIR / filename).read_text())


gap_allocation: TextClause = _q("gap_allocation.sql")
positions_latest: TextClause = _q("positions_latest.sql")
account_last_sync: TextClause = _q("account_last_sync.sql")
targets_active_count: TextClause = _q("targets_active_count.sql")
