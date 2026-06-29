"""Tests for the curated ticker→name map (P1.1)."""
from __future__ import annotations

from investor.services.ticker_names import TICKER_NAMES, name_for, names_for


def test_known_tickers_resolve() -> None:
    assert name_for("BTC") == "Grayscale Bitcoin Mini Trust ETF"  # the ADR-0029 case
    assert name_for("VOO") == "Vanguard S&P 500 ETF"
    assert name_for("NEE") == "NextEra Energy"


def test_unknown_ticker_returns_none() -> None:
    assert name_for("ZZZZ") is None


def test_names_for_drops_unknowns() -> None:
    out = names_for(["VOO", "ZZZZ", "MSFT"])
    assert out == {"VOO": "Vanguard S&P 500 ETF", "MSFT": "Microsoft"}
    assert "ZZZZ" not in out


def test_both_watchlists_fully_covered() -> None:
    # Current watchlists (acct 61 + 62) — every ticker should have a curated name.
    watchlist_union = {
        "VOO", "QQQ", "TQQQ", "BTC", "ISRG", "BRK.B", "AMZN", "GOOG", "MSFT", "MU",
        "NFLX", "ETH", "CEG", "PANW", "NVDA", "NEE", "TSLA",
    }
    missing = watchlist_union - TICKER_NAMES.keys()
    assert not missing, f"watchlist tickers missing a name: {sorted(missing)}"
