"""One-shot script: fetch 2 years of daily OHLCV bars from Alpaca and write to Parquet.

Uses StockHistoricalDataClient directly (not via BrokerAdapter — this is a bootstrap
script, not recurring app logic). Writes one file per ticker: data/bars/<TICKER>.parquet.

Usage:
    uv run python scripts/backfill_bars.py

Requires ALPACA_API_KEY and ALPACA_SECRET_KEY in .env (or environment).
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running as `python scripts/backfill_bars.py` from project root.
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
    bars_dir.mkdir(parents=True, exist_ok=True)

    client = StockHistoricalDataClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
    )

    end = date.today()
    start = end - timedelta(days=365 * 2 + 5)  # ~2 years + buffer

    print(f"Backfilling bars for {tickers} from {start} to {end}...")

    request = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed="iex",
    )

    try:
        bars = client.get_stock_bars(request)
    except Exception as exc:
        print(f"ERROR: Alpaca bar fetch failed: {exc}", file=sys.stderr)
        sys.exit(1)


    df = bars.df
    if df.empty:
        print("WARNING: No bars returned. Check tickers and API keys.")
        return

    # bars.df has a MultiIndex (symbol, timestamp) — reset to flat columns
    df = df.reset_index()

    written = 0
    for ticker in tickers:
        ticker_df = df[df["symbol"] == ticker].copy()
        if ticker_df.empty:
            print(f"  {ticker}: no data returned, skipping")
            continue
        out = bars_dir / f"{ticker}.parquet"
        ticker_df.to_parquet(out, index=False)
        print(f"  {ticker}: {len(ticker_df)} rows → {out}")
        written += 1

    print(f"\nDone. {written}/{len(tickers)} tickers written to {bars_dir}/")


if __name__ == "__main__":
    main()
