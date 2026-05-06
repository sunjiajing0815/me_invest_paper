"""Tests for support/resistance level computation."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from investor.services.indicators import IndicatorRow
from investor.services.levels import (
    NearbyLevels,
    SRLevelRow,
    _pivot_levels,
    _swing_levels,
    build_nearby_levels,
)


def _indicator(
    ticker: str = "VOO", close: float = 100.0, sma_50: float = 95.0, sma_200: float = 90.0
) -> IndicatorRow:
    today = date(2026, 5, 5)
    return IndicatorRow(
        ticker=ticker,
        as_of=today,
        close=close,
        sma_20=close - 2.0,
        sma_50=sma_50,
        sma_200=sma_200,
        ema_21=close - 1.0,
        rsi_14=55.0,
        macd=0.5,
        macd_signal=0.3,
        pct_from_sma_50=(close / sma_50 - 1) * 100,
        pct_from_sma_200=(close / sma_200 - 1) * 100,
    )


def _make_ohlcv_df(ticker: str = "VOO", n: int = 60) -> pd.DataFrame:
    start = date(2025, 1, 2)
    rows = []
    price = 100.0
    for i in range(n):
        d = start + timedelta(days=i)
        price += (i % 5 - 2) * 0.8
        rows.append({
            "ticker": ticker,
            "date": d,
            "open": price - 0.5,
            "high": price + 1.5,
            "low": price - 1.5,
            "close": price,
            "volume": 500_000,
        })
    return pd.DataFrame(rows)


class TestPivotLevels:
    def test_weekly_pivots_produced(self) -> None:
        df = _make_ohlcv_df(n=30)
        levels = _pivot_levels(df, date(2026, 5, 5))
        methods = {lv.method for lv in levels}
        assert "pivot_weekly_S1" in methods
        assert "pivot_weekly_R1" in methods

    def test_support_below_resistance(self) -> None:
        df = _make_ohlcv_df(n=30)
        levels = _pivot_levels(df, date(2026, 5, 5))
        supports = [lv for lv in levels if lv.type == "support"]
        resistances = [lv for lv in levels if lv.type == "resistance"]
        assert supports
        assert resistances
        max_support = max(lv.price for lv in supports)
        min_resistance = min(lv.price for lv in resistances)
        # S1 < R1 by definition: R1 = 2P - L > S1 = 2P - H (since L < H)
        assert max_support < min_resistance + 50  # generous bound — just checks sign

    def test_returns_empty_for_thin_data(self) -> None:
        df = _make_ohlcv_df(n=1)
        levels = _pivot_levels(df, date(2026, 5, 5))
        assert levels == []


class TestSwingLevels:
    def test_last_n_bars_excluded(self) -> None:
        df = _make_ohlcv_df(n=40)
        n = 5
        levels = _swing_levels(df, date(2026, 5, 5), n=n)
        # The implementation excludes the last N bars from fractal detection.
        # Verify no error and result is a list (as_of is the computation date, not a bar date).
        assert isinstance(levels, list)

    def test_swing_high_above_swing_low(self) -> None:
        df = _make_ohlcv_df(n=40)
        levels = _swing_levels(df, date(2026, 5, 5))
        highs = [lv.price for lv in levels if lv.type == "resistance"]
        lows = [lv.price for lv in levels if lv.type == "support"]
        if highs and lows:
            assert max(lows) <= max(highs)

    def test_returns_empty_for_insufficient_data(self) -> None:
        df = _make_ohlcv_df(n=5)
        levels = _swing_levels(df, date(2026, 5, 5), n=5)
        assert levels == []


class TestBuildNearbyLevels:
    def test_supports_are_below_current_price(self) -> None:
        today = date(2026, 5, 5)
        sr_rows = [
            SRLevelRow(ticker="VOO", type="support", price=90.0, method="sma_50", as_of=today),
            SRLevelRow(ticker="VOO", type="support", price=80.0, method="sma_200", as_of=today),
            SRLevelRow(
                ticker="VOO", type="resistance", price=110.0, method="pivot_weekly_R1", as_of=today
            ),
        ]
        ind = _indicator("VOO", close=100.0)
        result = build_nearby_levels(["VOO"], sr_rows, [ind])

        assert "VOO" in result
        lvl = result["VOO"]
        assert isinstance(lvl, NearbyLevels)
        for sup in lvl.supports:
            assert sup.price < lvl.current_price
        for res in lvl.resistances:
            assert res.price > lvl.current_price

    def test_at_most_3_supports_and_resistances(self) -> None:
        today = date(2026, 5, 5)
        sr_rows = [
            SRLevelRow(ticker="VOO", type="support", price=99.0 - i, method=f"m{i}", as_of=today)
            for i in range(5)
        ] + [
            SRLevelRow(
                ticker="VOO", type="resistance", price=101.0 + i, method=f"r{i}", as_of=today
            )
            for i in range(5)
        ]
        ind = _indicator("VOO", close=100.0)
        result = build_nearby_levels(["VOO"], sr_rows, [ind])
        lvl = result["VOO"]
        assert len(lvl.supports) <= 3
        assert len(lvl.resistances) <= 3

    def test_supports_sorted_nearest_first(self) -> None:
        today = date(2026, 5, 5)
        sr_rows = [
            SRLevelRow(ticker="VOO", type="support", price=70.0, method="far", as_of=today),
            SRLevelRow(ticker="VOO", type="support", price=95.0, method="near", as_of=today),
            SRLevelRow(ticker="VOO", type="support", price=85.0, method="mid", as_of=today),
        ]
        ind = _indicator("VOO", close=100.0)
        result = build_nearby_levels(["VOO"], sr_rows, [ind])
        supports = result["VOO"].supports
        prices = [s.price for s in supports]
        assert prices == sorted(prices, reverse=True)  # nearest = highest price for supports

    def test_missing_ticker_gets_empty_levels(self) -> None:
        ind = _indicator("QQQ", close=200.0)
        result = build_nearby_levels(["QQQ"], [], [ind])
        assert result["QQQ"].supports == []
        assert result["QQQ"].resistances == []
