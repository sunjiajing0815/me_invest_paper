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

import dataclasses
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
from .indicators import Candle, IndicatorRow

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
class LevelStats:
    """Candle-derived history for one nearby level (plans/ohlcv_decision_design.md).

    A *touch* is a bar whose low–high range included the level (the market traded
    there). ``closed_through_recently`` means a close beyond the level in its breaking
    direction (below a support / above a resistance) within the lookback — a broken,
    not tested, level."""

    last_touch: date | None
    touch_count: int
    touched_today: bool
    closed_through_recently: bool
    touch_volume_ratio: float | None  # mean volume on touch bars ÷ 20-bar mean volume


@dataclass(frozen=True)
class NearbyLevels:
    """Nearest support and resistance levels for a single ticker."""

    ticker: str
    current_price: float
    supports: list[SRLevelRow]      # up to 3 nearest below current price
    resistances: list[SRLevelRow]   # up to 3 nearest above current price
    # OHLCV decision layer (optional — None/{} when bars unavailable):
    current: Candle | None = None                                  # last full bar
    stats: dict[tuple[str, float], LevelStats] = dataclasses.field(default_factory=dict)

    def stats_for(self, level: SRLevelRow) -> LevelStats | None:
        return self.stats.get((level.method, round(level.price, 4)))


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


def compute_level_stats(
    bars: pd.DataFrame,
    level_price: float,
    level_type: str,
    *,
    touch_window_days: int = 30,
    broken_lookback_days: int = 10,
    volume_window: int = 20,
) -> LevelStats:
    """Candle-derived stats for one level over a bars frame (date/open/high/low/close/volume,
    ascending). Pure — see plans/ohlcv_decision_design.md for the semantics matrix."""
    if bars.empty:
        return LevelStats(None, 0, False, False, None)

    last_date = bars["date"].iloc[-1]
    touch_cutoff = last_date - pd.Timedelta(days=touch_window_days)
    broken_cutoff = last_date - pd.Timedelta(days=broken_lookback_days)

    touched = (bars["low"] <= level_price) & (bars["high"] >= level_price)
    in_window = bars["date"] >= touch_cutoff
    touch_bars = bars[touched & in_window]

    last_touch = None
    if not touch_bars.empty:
        lt = touch_bars["date"].iloc[-1]
        last_touch = lt.date() if hasattr(lt, "date") else lt

    last_bar = bars.iloc[-1]
    touched_today = bool(last_bar["low"] <= level_price <= last_bar["high"])

    recent = bars[bars["date"] >= broken_cutoff]
    if level_type == "support":
        closed_through = bool((recent["close"] < level_price).any())
    else:
        closed_through = bool((recent["close"] > level_price).any())

    ratio: float | None = None
    if not touch_bars.empty:
        base_vol = float(bars["volume"].tail(volume_window).mean())
        if base_vol > 0:
            ratio = float(touch_bars["volume"].mean()) / base_vol

    return LevelStats(
        last_touch=last_touch,
        touch_count=int(len(touch_bars)),
        touched_today=touched_today,
        closed_through_recently=closed_through,
        touch_volume_ratio=ratio,
    )


def _recent_bars_by_ticker(
    bars_dir: str, tickers: list[str], n: int = 60
) -> dict[str, pd.DataFrame]:
    """Last ``n`` bars per ticker (date asc) from the price_bar view. Empty dict on failure
    — level stats are an enhancement, never a reason to fail a run."""
    out: dict[str, pd.DataFrame] = {}
    try:
        with duckdb_conn(bars_dir) as con:
            for t in tickers:
                df = con.execute(
                    "SELECT date, open, high, low, close, volume FROM price_bar"
                    " WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                    [t, n],
                ).df()
                if not df.empty:
                    df = df.sort_values("date").reset_index(drop=True)
                    df["date"] = pd.to_datetime(df["date"])
                    out[t] = df
    except Exception as exc:
        logger.warning("level stats: bar fetch failed (%s) — stats skipped", exc)
        return {}
    return out


def build_nearby_levels(
    tickers: list[str],
    sr_rows: list[SRLevelRow],
    indicators: list[IndicatorRow],
    n: int = 3,
    max_distance_pct: float = 0.50,
    bars_dir: str | None = None,
    touch_window_days: int = 30,
    broken_lookback_days: int = 10,
) -> dict[str, NearbyLevels]:
    """Return NearbyLevels for each ticker: up to n supports below, n resistances above.

    ``max_distance_pct`` drops levels further than that fraction of current price away
    (default 50%). Defence-in-depth against bad bar data (e.g. an unadjusted corporate
    action leaving a phantom swing low far below current price) surfacing a meaningless
    "nearest" level — better to show no nearby level than a nonsense one.
    """
    ind_map = {r.ticker: r for r in indicators}
    result: dict[str, NearbyLevels] = {}
    bars_map = _recent_bars_by_ticker(bars_dir, tickers) if bars_dir else {}

    by_ticker: dict[str, list[SRLevelRow]] = {}
    for row in sr_rows:
        by_ticker.setdefault(row.ticker, []).append(row)

    for ticker in tickers:
        ind = ind_map.get(ticker)
        current_price = ind.close if ind else 0.0
        max_dist = max_distance_pct * current_price
        levels = by_ticker.get(ticker, [])

        supports = sorted(
            [
                lv for lv in levels
                if lv.type == "support"
                and lv.price < current_price
                and (current_price - lv.price) <= max_dist
            ],
            key=lambda lv: current_price - lv.price,
        )[:n]

        resistances = sorted(
            [
                lv for lv in levels
                if lv.type == "resistance"
                and lv.price > current_price
                and (lv.price - current_price) <= max_dist
            ],
            key=lambda lv: lv.price - current_price,
        )[:n]

        # OHLCV layer: last full candle (from the indicator row) + per-level candle
        # stats for the levels that survived filtering (≤ 2n per ticker — cheap).
        candle = ind.candle if ind else None
        stats: dict[tuple[str, float], LevelStats] = {}
        t_bars = bars_map.get(ticker)
        if t_bars is not None:
            for lv in supports + resistances:
                stats[(lv.method, round(lv.price, 4))] = compute_level_stats(
                    t_bars, lv.price, lv.type,
                    touch_window_days=touch_window_days,
                    broken_lookback_days=broken_lookback_days,
                )

        result[ticker] = NearbyLevels(
            ticker=ticker,
            current_price=current_price,
            supports=supports,
            resistances=resistances,
            current=candle,
            stats=stats,
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
