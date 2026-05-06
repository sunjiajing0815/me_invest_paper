"""Support/resistance levels service.

Three layered methods in increasing subjectivity:
  1. Classical pivot points (weekly + monthly) — formulaic
  2. Moving-average bands (SMA-20/50/200, EMA-21) — dynamic
  3. Swing highs/lows (fractal, n=5) — pattern-based

Results are persisted to the `sr_level` SQLite table. The `build_nearby_levels`
function selects the 3 nearest supports below and 3 nearest resistances above current
price for each ticker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import SRLevel
from .analytics import duckdb_conn
from .indicators import IndicatorRow

logger = logging.getLogger(__name__)

LevelType = Literal["support", "resistance"]


@dataclass(frozen=True)
class SRLevelRow:
    """Plain-data equivalent of the SRLevel ORM model — safe after session closes."""

    ticker: str
    type: LevelType
    price: float
    method: str
    as_of: date


@dataclass(frozen=True)
class NearbyLevels:
    """Nearest support and resistance levels for a single ticker."""

    ticker: str
    current_price: float
    supports: list[SRLevelRow]      # up to 3 nearest below current price
    resistances: list[SRLevelRow]   # up to 3 nearest above current price


# ---------------------------------------------------------------------------
# Internal computation helpers
# ---------------------------------------------------------------------------

def _pivot_levels(df: pd.DataFrame, as_of: date) -> list[SRLevelRow]:
    """Compute weekly + monthly classical pivot points from OHLCV data.

    Uses the prior period's H/L/C — NOT the current period.
    """
    if df.empty or len(df) < 2:
        return []

    levels: list[SRLevelRow] = []
    ticker = str(df["ticker"].iloc[0])

    # --- Weekly pivots: prior Monday-Friday bar window ---
    df_sorted = df.sort_values("date")
    # Group by ISO week; take the last full week (at least 3 bars)
    df_sorted = df_sorted.copy()
    df_sorted["iso_week"] = df_sorted["date"].apply(
        lambda d: d.isocalendar()[:2]  # (year, week)
    )
    week_groups = df_sorted.groupby("iso_week")
    weeks = sorted(week_groups.groups.keys())
    if len(weeks) >= 2:
        prior_week = week_groups.get_group(weeks[-2])
        ph = float(prior_week["high"].max())
        pl = float(prior_week["low"].min())
        pc = float(prior_week["close"].iloc[-1])
        p = (ph + pl + pc) / 3
        s1, s2 = 2 * p - ph, p - (ph - pl)
        r1, r2 = 2 * p - pl, p + (ph - pl)
        for val, method in [(s1, "pivot_weekly_S1"), (s2, "pivot_weekly_S2")]:
            levels.append(SRLevelRow(
                ticker=ticker, type="support", price=round(val, 4), method=method, as_of=as_of
            ))
        for val, method in [(r1, "pivot_weekly_R1"), (r2, "pivot_weekly_R2")]:
            levels.append(SRLevelRow(
                ticker=ticker, type="resistance", price=round(val, 4), method=method, as_of=as_of
            ))

    # --- Monthly pivots: prior calendar month ---
    df_sorted["month"] = df_sorted["date"].apply(lambda d: (d.year, d.month))
    month_groups = df_sorted.groupby("month")
    months = sorted(month_groups.groups.keys())
    if len(months) >= 2:
        prior_month = month_groups.get_group(months[-2])
        ph = float(prior_month["high"].max())
        pl = float(prior_month["low"].min())
        pc = float(prior_month["close"].iloc[-1])
        p = (ph + pl + pc) / 3
        s1, s2 = 2 * p - ph, p - (ph - pl)
        r1, r2 = 2 * p - pl, p + (ph - pl)
        for val, method in [(s1, "pivot_monthly_S1"), (s2, "pivot_monthly_S2")]:
            levels.append(SRLevelRow(
                ticker=ticker, type="support", price=round(val, 4), method=method, as_of=as_of
            ))
        for val, method in [(r1, "pivot_monthly_R1"), (r2, "pivot_monthly_R2")]:
            levels.append(SRLevelRow(
                ticker=ticker, type="resistance", price=round(val, 4), method=method, as_of=as_of
            ))

    return levels


def _ma_levels(indicator: IndicatorRow, as_of: date) -> list[SRLevelRow]:
    """MA-based dynamic S/R: MAs below price = support, above = resistance."""
    levels: list[SRLevelRow] = []
    current = indicator.close

    ma_map = {
        "sma_20": indicator.sma_20,
        "sma_50": indicator.sma_50,
        "sma_200": indicator.sma_200,
        "ema_21": indicator.ema_21,
    }
    for method, price in ma_map.items():
        if price is None:
            continue
        level_type: LevelType = "support" if price < current else "resistance"
        levels.append(SRLevelRow(
            ticker=indicator.ticker, type=level_type, price=round(price, 4),
            method=method, as_of=as_of,
        ))

    return levels


def _swing_levels(df: pd.DataFrame, as_of: date, n: int = 5) -> list[SRLevelRow]:
    """Fractal swing highs/lows.

    A bar is a swing high if its high exceeds all highs in the N bars before and after.
    Swing low mirrors. The last N bars are excluded — they cannot be confirmed yet.
    """
    if len(df) < 2 * n + 1:
        return []

    ticker = str(df["ticker"].iloc[0])
    df_sorted = df.sort_values("date").reset_index(drop=True)
    # Exclude the last N bars — they're unconfirmed
    confirmed = df_sorted.iloc[: len(df_sorted) - n]

    levels: list[SRLevelRow] = []
    for i in range(n, len(confirmed) - n):
        window = confirmed.iloc[i - n : i + n + 1]
        bar = confirmed.iloc[i]
        if float(bar["high"]) == float(window["high"].max()):
            levels.append(SRLevelRow(
                ticker=ticker,
                type="resistance",
                price=round(float(bar["high"]), 4),
                method="swing_high_5bar",
                as_of=as_of,
            ))
        if float(bar["low"]) == float(window["low"].min()):
            levels.append(SRLevelRow(
                ticker=ticker,
                type="support",
                price=round(float(bar["low"]), 4),
                method="swing_low_5bar",
                as_of=as_of,
            ))

    return levels


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_levels(
    tickers: list[str],
    indicators: list[IndicatorRow],
    bars_dir: str = "data/bars",
) -> list[SRLevelRow]:
    """Compute all S/R levels for every ticker and return as SRLevelRow list."""
    today = datetime.now(UTC).date()
    ind_map = {r.ticker: r for r in indicators}
    all_levels: list[SRLevelRow] = []

    with duckdb_conn(bars_dir) as con:
        for ticker in tickers:
            try:
                df: pd.DataFrame = con.execute(
                    "SELECT ticker, date, open, high, low, close"
                    " FROM price_bar WHERE ticker = ? ORDER BY date",
                    [ticker],
                ).df()
            except Exception as exc:
                logger.warning("compute_levels: bar fetch failed for %s: %s", ticker, exc)
                continue

            if df.empty:
                logger.debug("compute_levels: no bars for %s", ticker)
                continue

            all_levels.extend(_pivot_levels(df, today))
            if ticker in ind_map:
                all_levels.extend(_ma_levels(ind_map[ticker], today))
            all_levels.extend(_swing_levels(df, today))

    return all_levels


def persist_levels(session: Session, rows: list[SRLevelRow]) -> None:
    """Upsert SRLevelRow list into the sr_level table.

    Uses SQLite's INSERT OR REPLACE semantics via the UniqueConstraint on (ticker, method, as_of).
    """
    if not rows:
        return
    now = datetime.now(UTC)
    for row in rows:
        stmt = (
            sqlite_insert(SRLevel)
            .values(
                ticker=row.ticker,
                type=row.type,
                price=row.price,
                method=row.method,
                as_of=row.as_of,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=["ticker", "method", "as_of"],
                set_={"price": row.price, "type": row.type},
            )
        )
        session.execute(stmt)
    session.flush()


def build_nearby_levels(
    tickers: list[str],
    sr_rows: list[SRLevelRow],
    indicators: list[IndicatorRow],
    n: int = 3,
) -> dict[str, NearbyLevels]:
    """Return NearbyLevels for each ticker: up to n supports below, n resistances above."""
    ind_map = {r.ticker: r for r in indicators}
    result: dict[str, NearbyLevels] = {}

    by_ticker: dict[str, list[SRLevelRow]] = {}
    for row in sr_rows:
        by_ticker.setdefault(row.ticker, []).append(row)

    for ticker in tickers:
        ind = ind_map.get(ticker)
        current_price = ind.close if ind else 0.0
        levels = by_ticker.get(ticker, [])

        supports = sorted(
            [lv for lv in levels if lv.type == "support" and lv.price < current_price],
            key=lambda lv: current_price - lv.price,
        )[:n]

        resistances = sorted(
            [lv for lv in levels if lv.type == "resistance" and lv.price > current_price],
            key=lambda lv: lv.price - current_price,
        )[:n]

        result[ticker] = NearbyLevels(
            ticker=ticker,
            current_price=current_price,
            supports=supports,
            resistances=resistances,
        )

    return result


def get_active_targets_id(session: Session) -> int | None:
    """Return the id of the most recently inserted active TargetAllocation row."""
    from ..models import TargetAllocation

    row = (
        session.execute(
            select(TargetAllocation.id)
            .where(TargetAllocation.effective_to.is_(None))
            .order_by(TargetAllocation.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return row
