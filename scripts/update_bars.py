"""Append today's bar to each existing Parquet file in data/bars/.

Run daily (or wire into scheduler in Phase 2) after market close.
Skips tickers whose Parquet doesn't exist — run backfill_bars.py first.

Usage:
    uv run python scripts/update_bars.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from investor.config import Settings, load_targets


def main() -> None:
    settings = Settings()
    targets = load_targets(settings.targets_path)
    tickers = targets.watchlist

    bars_dir = Path("data/bars")

    client = StockHistoricalDataClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
    )

    today = date.today()
    # fetch yesterday and today to handle timezone edge cases
    start = today - timedelta(days=1)

    request = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        start=start,
        end=today,
        feed="iex",
    )

    try:
        bars = client.get_stock_bars(request)
    except Exception as exc:
        print(f"ERROR: Alpaca bar fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)

    import pandas as pd

    df = bars.df
    if df.empty:
        print("No bars returned — market may be closed.")
        return

    df = df.reset_index()

    for ticker in tickers:
        out = bars_dir / f"{ticker}.parquet"
        if not out.exists():
            print(f"  {ticker}: {out} not found — run backfill_bars.py first, skipping")
            continue

        ticker_new = df[df["symbol"] == ticker].copy()
        if ticker_new.empty:
            print(f"  {ticker}: no new bars, skipping")
            continue

        existing = pd.read_parquet(out)
        combined = pd.concat([existing, ticker_new], ignore_index=True)
        combined = combined.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        combined.to_parquet(out, index=False)
        new_rows = len(combined) - len(existing)
        print(f"  {ticker}: +{new_rows} row(s) → {len(combined)} total")

    print("Done.")


if __name__ == "__main__":
    main()
