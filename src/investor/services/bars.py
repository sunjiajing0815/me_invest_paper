"""Bar update service: smart backfill + incremental append to Parquet files."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKFILL_DAYS = 365 * 2 + 5  # ~2 years + buffer for weekends/holidays


def update_bars(
    tickers: list[str],
    api_key: str,
    secret_key: str,
    bars_dir: str = "data/bars",
) -> None:
    """Backfill or incrementally update OHLCV bar Parquet files.

    For each ticker:
    - No existing file → fetches full 2-year history and writes a new file.
    - File exists → fetches from its last bar date to today and appends.

    All tickers are batched into a single Alpaca API call using the earliest
    needed start date, then filtered per-ticker before writing.
    """
    import pandas as pd
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    bars_path = Path(bars_dir)
    bars_path.mkdir(parents=True, exist_ok=True)
    today = date.today()

    # Determine start date and load any existing data per ticker.
    ticker_starts: dict[str, date] = {}
    existing_dfs: dict[str, pd.DataFrame] = {}

    for ticker in tickers:
        out = bars_path / f"{ticker}.parquet"
        if out.exists():
            df = pd.read_parquet(out)
            existing_dfs[ticker] = df
            last_date = pd.to_datetime(df["timestamp"]).max().date()
            ticker_starts[ticker] = last_date  # fetch from last bar (dedup handles overlap)
            logger.debug("update_bars: %s last bar %s → incremental", ticker, last_date)
        else:
            ticker_starts[ticker] = today - timedelta(days=_BACKFILL_DAYS)
            logger.info("update_bars: %s no file found → backfilling 2 years", ticker)

    overall_start = min(ticker_starts.values())
    logger.info(
        "update_bars: fetching %d ticker(s) from %s to %s",
        len(tickers), overall_start, today,
    )

    client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    request = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        start=overall_start,
        end=today,
        feed="iex",
    )
    bars = client.get_stock_bars(request)
    new_df = bars.df
    if new_df.empty:
        logger.info("update_bars: no bars returned — market may be closed")
        return

    new_df = new_df.reset_index()

    for ticker in tickers:
        out = bars_path / f"{ticker}.parquet"
        ticker_new = new_df[new_df["symbol"] == ticker].copy()
        if ticker_new.empty:
            logger.debug("update_bars: no new bars for %s", ticker)
            continue

        if ticker in existing_dfs:
            combined = pd.concat([existing_dfs[ticker], ticker_new], ignore_index=True)
            combined = combined.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
            added = len(combined) - len(existing_dfs[ticker])
        else:
            combined = ticker_new.sort_values("timestamp")
            added = len(combined)

        combined.to_parquet(out, index=False)
        logger.info("update_bars: %s +%d row(s) → %d total", ticker, added, len(combined))
