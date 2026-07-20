"""OHLCV candle semantics for levels (plans/ohlcv_decision_design.md, steps 1–2).

Touch / tested-and-held / broken / reclaimed matrix on synthetic bars, plus the
Candle plumbing through IndicatorRow and NearbyLevels.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from investor.services.indicators import Candle, IndicatorRow
from investor.services.levels import (
    LevelStats,
    SRLevelRow,
    build_nearby_levels,
    compute_level_stats,
)

_END = date(2026, 7, 17)


def _bars(rows: list[tuple[int, float, float, float, float, float]]) -> pd.DataFrame:
    """rows: (days_ago, open, high, low, close, volume) — returned sorted by date asc."""
    data = [
        {"date": _END - timedelta(days=ago), "open": o, "high": h, "low": lo,
         "close": c, "volume": v}
        for ago, o, h, lo, c, v in rows
    ]
    return pd.DataFrame(sorted(data, key=lambda r: r["date"]))


def _flat(days: int, price: float = 110.0, vol: float = 1000.0):
    """Filler bars well above the level under test."""
    return [(d, price, price + 1, price - 1, price, vol) for d in days_range(days)]


def days_range(n: int, start: int = 3) -> list[int]:
    return list(range(start, start + n))


# ── touch counting ────────────────────────────────────────────────────────────

def test_touch_counted_when_range_includes_level() -> None:
    # level 100: bar (low 99, high 101) touches; bar far above doesn't.
    bars = _bars([(1, 110, 111, 99, 105, 1000)] + _flat(25))
    st = compute_level_stats(bars, 100.0, "support")
    assert st.touch_count == 1
    assert st.last_touch == _END - timedelta(days=1)


def test_touch_outside_window_not_counted() -> None:
    bars = _bars([(45, 110, 111, 99, 105, 1000)] + _flat(25))  # touch 45d ago
    st = compute_level_stats(bars, 100.0, "support", touch_window_days=30)
    assert st.touch_count == 0 and st.last_touch is None


def test_touched_today_from_last_bar() -> None:
    bars = _bars(_flat(25) + [(0, 102, 103, 99.5, 101, 1000)])
    st = compute_level_stats(bars, 100.0, "support")
    assert st.touched_today is True


# ── tested-and-held vs broken vs reclaimed ────────────────────────────────────

def test_tested_and_held_is_not_broken() -> None:
    # low pierced 100 but close reclaimed → touch, NOT closed_through.
    bars = _bars([(2, 103, 104, 98.0, 101.5, 1500)] + _flat(25))
    st = compute_level_stats(bars, 100.0, "support")
    assert st.touch_count == 1
    assert st.closed_through_recently is False


def test_support_closed_below_recently_is_broken() -> None:
    bars = _bars([(3, 101, 102, 97.0, 98.5, 1500)] + _flat(25))  # close 98.5 < 100
    st = compute_level_stats(bars, 100.0, "support", broken_lookback_days=10)
    assert st.closed_through_recently is True


def test_old_break_reclaimed_not_flagged() -> None:
    # closed below 100 twenty days ago, back above since → outside lookback → not broken.
    bars = _bars([(20, 101, 102, 97.0, 98.5, 1500)] + _flat(15))
    st = compute_level_stats(bars, 100.0, "support", broken_lookback_days=10)
    assert st.closed_through_recently is False


def test_resistance_broken_mirrors_upward() -> None:
    # resistance 120: close above it within lookback → broken.
    bars = _bars([(2, 118, 122, 117, 121.0, 1500)] + _flat(25))
    st = compute_level_stats(bars, 120.0, "resistance", broken_lookback_days=10)
    assert st.closed_through_recently is True


# ── volume ratio ──────────────────────────────────────────────────────────────

def test_touch_volume_ratio_above_one_for_heavy_touches() -> None:
    # touch bars at 3000 vol vs filler 1000 → ratio ≈ 3 / ~1.1.
    bars = _bars([(1, 110, 111, 99, 105, 3000), (2, 110, 111, 99, 104, 3000)] + _flat(20))
    st = compute_level_stats(bars, 100.0, "support")
    assert st.touch_volume_ratio is not None and st.touch_volume_ratio > 1.5


def test_no_touches_no_volume_ratio() -> None:
    bars = _bars(_flat(25))
    st = compute_level_stats(bars, 100.0, "support")
    assert st.touch_count == 0 and st.touch_volume_ratio is None


# ── plumbing: Candle on IndicatorRow / NearbyLevels ──────────────────────────

def _ind(ticker: str = "QQQ", close: float = 105.0, **ohlv: float) -> IndicatorRow:
    return IndicatorRow(
        ticker=ticker, as_of=_END, close=close, sma_20=None, sma_50=None, sma_200=None,
        ema_21=None, rsi_14=None, macd=None, macd_signal=None,
        pct_from_sma_50=None, pct_from_sma_200=None, **ohlv,
    )


def test_nearby_levels_carries_candle_when_indicator_has_ohlv() -> None:
    ind = _ind(open=104.0, high=106.0, low=103.0, volume=5000.0)
    sr = [SRLevelRow(ticker="QQQ", type="support", price=100.0, method="sma_50",
                     as_of=_END)]
    out = build_nearby_levels(["QQQ"], sr, [ind])
    nb = out["QQQ"]
    assert isinstance(nb.current, Candle)
    assert nb.current.low == 103.0 and nb.current.close == 105.0
    assert nb.current_price == 105.0     # legacy field intact


def test_nearby_levels_candle_none_without_ohlv() -> None:
    out = build_nearby_levels(["QQQ"], [], [_ind()])
    assert out["QQQ"].current is None


def test_stats_attached_from_bars_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Write a real parquet so the duckdb path runs end-to-end.
    bars = _bars([(1, 110, 111, 99, 105, 3000)] + _flat(25))
    bars.insert(0, "symbol", "QQQ")
    bars = bars.rename(columns={"date": "timestamp"})
    bars["trade_count"] = 1.0
    bars["vwap"] = bars["close"]
    bars.to_parquet(tmp_path / "QQQ.parquet", index=False)

    ind = _ind(open=104.0, high=106.0, low=103.0, volume=5000.0)
    sr = [SRLevelRow(ticker="QQQ", type="support", price=100.0, method="sma_50",
                     as_of=_END)]
    out = build_nearby_levels(["QQQ"], sr, [ind], bars_dir=str(tmp_path))
    nb = out["QQQ"]
    st = nb.stats_for(sr[0])
    assert isinstance(st, LevelStats)
    assert st.touch_count >= 1


def test_stats_absent_without_bars_dir() -> None:
    ind = _ind(open=104.0, high=106.0, low=103.0, volume=5000.0)
    sr = [SRLevelRow(ticker="QQQ", type="support", price=100.0, method="sma_50",
                     as_of=_END)]
    out = build_nearby_levels(["QQQ"], sr, [ind])
    assert out["QQQ"].stats_for(sr[0]) is None
