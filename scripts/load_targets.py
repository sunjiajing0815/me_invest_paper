#!/usr/bin/env python
"""Seed target_allocation table from targets.yaml.

Uses close-and-insert versioning: closes existing open rows, inserts new ones.
Run: uv run python scripts/load_targets.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from investor.config import Settings, load_targets
from investor.db import init_db
from investor.models import TargetAllocation
from sqlalchemy.orm import Session


def main() -> None:
    settings = Settings()
    engine = init_db(settings.duckdb_path)
    targets = load_targets(settings.targets_path)
    now = datetime.now(UTC)

    with Session(engine) as sess:
        open_rows = (
            sess.query(TargetAllocation)
            .filter(TargetAllocation.effective_to.is_(None))
            .all()
        )
        for row in open_rows:
            row.effective_to = now
        sess.flush()

        new_rows = [
            TargetAllocation(
                ticker=t.ticker,
                target_pct=t.pct,
                band_low_pct=t.band_low,
                band_high_pct=t.band_high,
                effective_from=now,
                effective_to=None,
            )
            for t in targets.targets
        ]
        sess.add_all(new_rows)
        sess.commit()

    print(f"Loaded {len(new_rows)} targets from {settings.targets_path}")
    print(f"Closed {len(open_rows)} previous target rows")
    for t in targets.targets:
        print(f"  {t.ticker}: {t.pct}% [{t.band_low}%, {t.band_high}%]")


if __name__ == "__main__":
    main()
