"""Append today's bar to each existing Parquet file in data/bars/.

Run daily (or wire into scheduler in Phase 2) after market close.
Skips tickers whose Parquet doesn't exist — run backfill_bars.py first.

Usage:
    uv run python scripts/update_bars.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from investor.config import Settings, load_targets
from investor.services.bars import update_bars


def main() -> None:
    settings = Settings()
    targets = load_targets(settings.targets_path)
    try:
        update_bars(
            tickers=targets.watchlist,
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            bars_dir=settings.bars_dir,
        )
        print("Done.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
