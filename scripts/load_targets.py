#!/usr/bin/env python
"""Seed target_allocation table from targets.yaml.

Uses close-and-insert versioning: closes existing open rows, inserts new ones.
Skips the write entirely if targets are identical to the current open rows.
Run: uv run python scripts/load_targets.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from investor.config import Settings, TargetsConfig, load_targets
from investor.db import init_db
from investor.models import TargetAllocation
from sqlalchemy.orm import Session


def _targets_unchanged(existing: list[TargetAllocation], new: TargetsConfig) -> bool:
    if len(existing) != len(new.targets):
        print(f"  count changed: {len(existing)} existing vs {len(new.targets)} new")
        return False
    existing_map = {r.ticker: r for r in existing}
    for t in new.targets:
        r = existing_map.get(t.ticker)
        if r is None:
            print(f"  new ticker not in DB: {t.ticker}")
            return False
        db_pct = round(float(r.target_pct), 6)
        db_low = round(float(r.band_low_pct), 6)
        db_high = round(float(r.band_high_pct), 6)
        if db_pct != t.pct or db_low != t.band_low or db_high != t.band_high:
            print(
                f"  {t.ticker} changed: "
                f"pct {db_pct}->{t.pct}, "
                f"band [{db_low},{db_high}]->[{t.band_low},{t.band_high}]"
            )
            return False
    return True


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

        print(f"Checking {len(open_rows)} open rows vs {len(targets.targets)} targets in YAML...")
        if _targets_unchanged(open_rows, targets):
            print("Targets unchanged — no update needed")
            for t in targets.targets:
                print(f"  {t.ticker}: {t.pct}% [{t.band_low}%, {t.band_high}%]")
            return

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
