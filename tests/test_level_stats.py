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


# ── step 3: broken-level guard in anchor selection ────────────────────────────

def _nb_with_stats(supports: list[SRLevelRow], stats: dict, current_price: float = 105.0):  # type: ignore[no-untyped-def]
    from investor.services.levels import NearbyLevels
    return NearbyLevels(ticker="QQQ", current_price=current_price, supports=supports,
                        resistances=[], stats=stats)


def _st(broken: bool = False, touches: int = 0, today: bool = False,
        vol: float | None = None) -> LevelStats:
    return LevelStats(last_touch=None, touch_count=touches, touched_today=today,
                      closed_through_recently=broken, touch_volume_ratio=vol)


def _sr(method: str, price: float) -> SRLevelRow:
    return SRLevelRow(ticker="QQQ", type="support", price=price, method=method, as_of=_END)


def test_broken_fallback_support_skipped_for_next() -> None:
    from investor.services.suggest import _select_buy_anchor
    s1, s2 = _sr("sma_20", 102.0), _sr("sma_50", 100.0)
    nb = _nb_with_stats([s1, s2], {("sma_20", 102.0): _st(broken=True),
                                   ("sma_50", 100.0): _st(touches=2)})
    anchor, reason = _select_buy_anchor(nb, None, 15.0)
    assert anchor is not None and anchor.method == "sma_50"  # broken sma_20 skipped


def test_all_supports_broken_no_anchor() -> None:
    from investor.services.suggest import _select_buy_anchor
    s1 = _sr("sma_20", 102.0)
    nb = _nb_with_stats([s1], {("sma_20", 102.0): _st(broken=True)})
    anchor, reason = _select_buy_anchor(nb, None, 15.0)
    assert anchor is None
    assert "broken" in (reason or "")


def test_broken_scored_level_excluded_falls_back() -> None:
    from investor.services.llm_levels import ScoredLevel
    from investor.services.suggest import _select_buy_anchor
    scored = [ScoredLevel(method="ema_21", price=103.0, type="support",
                          confidence=0.8, rationale="t")]
    fallback = _sr("sma_50", 100.0)
    nb = _nb_with_stats([fallback], {("ema_21", 103.0): _st(broken=True),
                                     ("sma_50", 100.0): _st()})
    anchor, _ = _select_buy_anchor(nb, scored, 15.0)
    assert anchor is not None and anchor.method == "sma_50"  # scored-but-broken skipped


def test_no_stats_fail_open_unchanged() -> None:
    from investor.services.suggest import _select_buy_anchor
    s1 = _sr("sma_20", 102.0)
    nb = _nb_with_stats([s1], {})  # no stats at all
    anchor, _ = _select_buy_anchor(nb, None, 15.0)
    assert anchor is not None and anchor.method == "sma_20"


def test_reason_carries_touch_history() -> None:
    from investor.services.suggest import _level_history_note
    nb = _nb_with_stats([_sr("sma_50", 100.0)],
                        {("sma_50", 100.0): _st(touches=3, today=True, vol=1.4)})
    note = _level_history_note(nb, "sma_50", 100.0)
    assert "tested 3× in 30d" in note
    assert "1.4× vol" in note
    assert "touched today" in note


def test_reason_note_empty_without_stats() -> None:
    from investor.services.suggest import _level_history_note
    nb = _nb_with_stats([_sr("sma_50", 100.0)], {})
    assert _level_history_note(nb, "sma_50", 100.0) == ""


def test_generate_suggestions_reason_includes_history() -> None:
    from investor.services.daily_report import AccountSnapshot
    from investor.services.gap import GapRow
    from investor.services.levels import NearbyLevels
    from investor.services.suggest import generate_suggestions
    sr = _sr("sma_50", 100.0)
    nb = NearbyLevels(ticker="QQQ", current_price=105.0, supports=[sr], resistances=[],
                      stats={("sma_50", 100.0): _st(touches=3, today=True, vol=1.4)})
    gap = GapRow(ticker="QQQ", current_pct=20.0, target_pct=30.0, gap_pct=10.0,
                 gap_usd=10_000.0, band_status="under")
    out, _ = generate_suggestions(
        gap_rows=[gap], nearby_levels={"QQQ": nb},
        account=AccountSnapshot("alpaca", "paper", 50_000.0, 100_000.0),
    )
    assert len(out) == 1
    assert "tested 3× in 30d" in out[0].reason
    assert "touched today" in out[0].reason
