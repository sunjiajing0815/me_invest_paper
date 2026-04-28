#!/usr/bin/env python
"""CLI: pull positions from broker and persist to SQLite.

Run: uv run python scripts/sync_positions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from investor.brokers import make_adapter
from investor.config import Settings
from investor.db import init_db, session_scope
from investor.services.snapshot import take_snapshot


def main() -> None:
    settings = Settings()
    init_db(settings.sqlite_path)
    adapter = make_adapter(settings)
    with session_scope() as session:
        n = take_snapshot(adapter, session, settings)
    print(f"Wrote {n} position rows + 1 broker_account row")


if __name__ == "__main__":
    main()
