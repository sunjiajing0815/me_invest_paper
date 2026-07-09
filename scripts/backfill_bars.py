"""CLI: force a full bar sync for all watchlist tickers.

Delegates to services/bars.update_bars(), which automatically backfills 2 years
for any ticker whose Parquet file is missing and does an incremental update otherwise.

Usage:
    uv run python scripts/backfill_bars.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from investor.config import Settings, load_targets  # noqa: E402
from investor.services.bars import update_bars  # noqa: E402


def main() -> None:
    settings = Settings()
    targets = load_targets(settings.targets_path)
    print(f"Syncing bars for: {targets.watchlist}")
    try:
        update_bars(
            targets.watchlist,
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            bars_dir=settings.bars_dir,
        )
        print("Done.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
