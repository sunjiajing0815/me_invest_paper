#!/usr/bin/env python
"""P1.6 Phase A: quantify SPLIT-only vs ALL (split+dividend) bar adjustment.

Read-only analysis to decide whether `services/bars.py` should move from `Adjustment.SPLIT`
to `Adjustment.ALL` for the swing-low S/R detectors (ADR-0029 follow-up). For each ticker it
fetches ~2y daily bars under both adjustments and reports the maximum divergence (= cumulative
dividend drag at the oldest bar) and how far the 2-year minimum low shifts. A swing low can move
at most by that divergence; compare against the suggestion engine's ~15% distance band.

Run: docker compose exec app uv run python scripts/compare_dividend_adjustment.py
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from investor.config import Settings  # noqa: E402

# Highest-yield names actually on the watchlists (61 ∪ 62); growth/no-div names are unaffected.
_TICKERS = ["NEE", "VOO", "QQQ", "ISRG", "MSFT", "BTC"]
_BACKFILL_DAYS = 365 * 2 + 5


def _fetch(client, symbols, adjustment, start, end):  # type: ignore[no-untyped-def]
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    req = StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
        start=datetime.combine(start, time.min), end=datetime.combine(end, time.min),
        feed=DataFeed.IEX, adjustment=adjustment,
    )
    return client.get_stock_bars(req).df


def main() -> None:
    from alpaca.data.enums import Adjustment
    from alpaca.data.historical import StockHistoricalDataClient

    s = Settings()
    client = StockHistoricalDataClient(api_key=s.alpaca_api_key, secret_key=s.alpaca_secret_key)
    end = datetime.now().date()
    start = end - timedelta(days=_BACKFILL_DAYS)

    split = _fetch(client, _TICKERS, Adjustment.SPLIT, start, end).reset_index()
    allj = _fetch(client, _TICKERS, Adjustment.ALL, start, end).reset_index()

    print(f"Dividend-adjustment comparison ({start} → {end}); SPLIT vs ALL\n")
    print(f"{'ticker':7} {'oldest Δ%':>9} {'minlow SPLIT':>13} {'minlow ALL':>11} {'minlow Δ%':>9}")
    print("-" * 54)
    for t in _TICKERS:
        sp = split[split["symbol"] == t].sort_values("timestamp")
        al = allj[allj["symbol"] == t].sort_values("timestamp")
        if sp.empty or al.empty:
            print(f"{t:7} (no data)")
            continue
        # Max divergence is at the oldest bar (full accumulated dividend back-adjustment).
        oldest_div = (al.iloc[0]["close"] / sp.iloc[0]["close"] - 1.0) * 100
        ml_sp = sp["low"].min()
        ml_al = al["low"].min()
        ml_delta = (ml_al / ml_sp - 1.0) * 100
        print(f"{t:7} {oldest_div:>8.2f}% {ml_sp:>13.2f} {ml_al:>11.2f} {ml_delta:>8.2f}%")

    print(
        "\nInterpretation: 'oldest Δ%' is the largest a 2-year-old swing low could shift under "
        "ALL.\nThe suggestion engine anchors within ~15% of current price (build_nearby_levels "
        "also drops\nlevels >50% away). If the shifts are small relative to that band, SPLIT-only "
        "is fine."
    )


if __name__ == "__main__":
    main()
