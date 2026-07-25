"""Technical indicators service: SMA, EMA, RSI, MACD per ticker.

SMAs are computed via a single DuckDB window-function pass (vectorised, all tickers at once).
EMA/RSI/MACD are computed per-ticker via pandas-ta because they are recursive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import pandas as pd

from .analytics import duckdb_conn

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candle:
    """One daily OHLCV bar — the decision layer's view of 'current price' with its full
    range (plans/pre_phase5_features_design.md)."""

    as_of: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class IndicatorRow:
    ticker: str
    as_of: date
    close: float
    sma_20: float | None
    sma_50: float | None
    sma_200: float | None
    ema_21: float | None
    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    pct_from_sma_50: float | None
    pct_from_sma_200: float | None
    # Last bar's full candle (OHLCV decision logic). Optional so fabricated rows in
    # tests and older callers keep working; None = candle unavailable.
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None

    @property
    def candle(self) -> Candle | None:
        """The last bar as a Candle, when the OHLV fields are populated."""
        if self.open is None or self.high is None or self.low is None or self.volume is None:
            return None
        return Candle(as_of=self.as_of, open=self.open, high=self.high, low=self.low,
                      close=self.close, volume=self.volume)


def _nan_to_none(value: object) -> float | None:
    """Convert NaN/None to None; return float otherwise."""
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def compute_indicators(tickers: list[str], bars_dir: str = "data/bars") -> list[IndicatorRow]:
    """Return one IndicatorRow per ticker (latest available date).

    Requires Parquet files at bars_dir/<TICKER>.parquet with ≥ 200 rows for SMA-200.
    Returns an empty list if no bar data is found.
    """
    import pandas_ta as ta  # imported here to keep startup fast

    rows: list[IndicatorRow] = []

    with duckdb_conn(bars_dir) as con:
        # One bulk pass for all SMA variants — DuckDB window functions are vectorised.
        sma_sql = """
            WITH augmented AS (
                SELECT
                    ticker, date, open, high, low, close, volume,
                    AVG(close) OVER (
                        PARTITION BY ticker ORDER BY date
                        ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)  AS sma_20,
                    AVG(close) OVER (
                        PARTITION BY ticker ORDER BY date
                        ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)  AS sma_50,
                    AVG(close) OVER (
                        PARTITION BY ticker ORDER BY date
                        ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sma_200,
                    ROW_NUMBER() OVER (
                        PARTITION BY ticker ORDER BY date DESC)      AS rn
                FROM price_bar
                WHERE ticker = ANY(?)
            )
            SELECT ticker, date, open, high, low, close, volume, sma_20, sma_50, sma_200
            FROM augmented
            WHERE rn = 1
        """
        try:
            sma_rows = con.execute(sma_sql, [tickers]).fetchall()
        except Exception as exc:
            logger.warning("compute_indicators: SMA query failed: %s", exc)
            return []

        if not sma_rows:
            logger.warning("compute_indicators: no bar data found for %s", tickers)
            return []

        for ticker, as_of, open_, high, low, close, volume, sma_20, sma_50, sma_200 in sma_rows:
            try:
                df: pd.DataFrame = con.execute(
                    "SELECT date, close FROM price_bar WHERE ticker = ? ORDER BY date",
                    [ticker],
                ).df()
            except Exception as exc:
                logger.warning("compute_indicators: series fetch failed for %s: %s", ticker, exc)
                continue

            ema_21 = _nan_to_none(ta.ema(df["close"], length=21).iloc[-1])
            rsi_14 = _nan_to_none(ta.rsi(df["close"], length=14).iloc[-1])

            macd_val: float | None = None
            macd_sig: float | None = None
            try:
                macd_df = ta.macd(df["close"])
                if macd_df is not None and not macd_df.empty:
                    macd_val = _nan_to_none(macd_df["MACD_12_26_9"].iloc[-1])
                    macd_sig = _nan_to_none(macd_df["MACDs_12_26_9"].iloc[-1])
            except Exception:
                pass

            sma_50_f = _nan_to_none(sma_50)
            sma_200_f = _nan_to_none(sma_200)
            close_f = float(close)

            rows.append(IndicatorRow(
                ticker=ticker,
                as_of=as_of,
                close=close_f,
                sma_20=_nan_to_none(sma_20),
                sma_50=sma_50_f,
                sma_200=sma_200_f,
                ema_21=ema_21,
                rsi_14=rsi_14,
                macd=macd_val,
                macd_signal=macd_sig,
                pct_from_sma_50=(close_f / sma_50_f - 1) * 100 if sma_50_f else None,
                pct_from_sma_200=(close_f / sma_200_f - 1) * 100 if sma_200_f else None,
                open=_nan_to_none(open_),
                high=_nan_to_none(high),
                low=_nan_to_none(low),
                volume=_nan_to_none(volume),
            ))

    return rows
