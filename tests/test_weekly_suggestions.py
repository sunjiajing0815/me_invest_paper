"""Tests for src/investor/jobs/weekly_suggestions.py."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from investor.jobs.weekly_suggestions import score_all_tickers_parallel
from investor.services.llm_levels import ScoredLevel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tickers(n: int) -> list[str]:
    symbols = ["AAPL", "MSFT", "GOOG", "AMZN", "TSLA", "NVDA", "META", "NFLX"]
    return symbols[:n]


# ---------------------------------------------------------------------------
# Parallel scoring tests
# ---------------------------------------------------------------------------


class TestScoreAllTickersParallel:
    def test_parallel_scoring_wall_clock(self) -> None:
        """Smoke row 12: 8 tickers with 0.2s mock delay + 4 workers should complete < 0.8s."""
        tickers = _make_tickers(8)
        mock_llm = MagicMock()

        def _slow_score(*args: object, **kwargs: object) -> list[ScoredLevel]:
            time.sleep(0.2)
            return []

        with patch(
            "investor.jobs.weekly_suggestions.score_levels_for_ticker",
            side_effect=_slow_score,
        ), patch(
            "investor.jobs.weekly_suggestions.session_scope",
        ) as mock_session_scope:
            # Make session_scope a context manager that yields a MagicMock
            mock_sess = MagicMock()
            mock_session_scope.return_value.__enter__ = MagicMock(return_value=mock_sess)
            mock_session_scope.return_value.__exit__ = MagicMock(return_value=False)

            t0 = time.monotonic()
            result = score_all_tickers_parallel(
                tickers=tickers,
                sr_rows=[],
                llm=mock_llm,
                bars_dir="data/bars",
                recent_news_by_ticker={},
                prompt_version="v2",
                max_workers=4,
            )
            elapsed = time.monotonic() - t0

        # With 4 workers and 0.2s per task: 8 tasks / 4 workers = 2 batches → ~0.4s
        # Allow generous headroom for CI overhead
        assert elapsed < 0.8, f"Expected < 0.8s, got {elapsed:.2f}s"
        assert set(result.keys()) == set(tickers)

    def test_parallel_scoring_failure_fallback(self) -> None:
        """One ticker raises → gets [] in result; others succeed normally."""
        tickers = _make_tickers(4)
        failing_ticker = tickers[1]  # "MSFT" will fail
        mock_llm = MagicMock()

        call_count = {"n": 0}

        def _maybe_raise(*args: object, **kwargs: object) -> list[ScoredLevel]:
            call_count["n"] += 1
            ticker_arg = kwargs.get("ticker", "")
            if ticker_arg == failing_ticker:
                raise RuntimeError(f"Simulated failure for {failing_ticker}")
            return []

        with patch(
            "investor.jobs.weekly_suggestions.score_levels_for_ticker",
            side_effect=_maybe_raise,
        ), patch(
            "investor.jobs.weekly_suggestions.session_scope",
        ) as mock_session_scope:
            mock_sess = MagicMock()
            mock_session_scope.return_value.__enter__ = MagicMock(return_value=mock_sess)
            mock_session_scope.return_value.__exit__ = MagicMock(return_value=False)

            result = score_all_tickers_parallel(
                tickers=tickers,
                sr_rows=[],
                llm=mock_llm,
                bars_dir="data/bars",
                recent_news_by_ticker={},
                prompt_version="v2",
                max_workers=4,
            )

        # All tickers appear in result
        assert set(result.keys()) == set(tickers)
        # Failing ticker gets empty list fallback
        assert result[failing_ticker] == []
        # Other tickers also get [] (the mock returns [])
        for t in tickers:
            if t != failing_ticker:
                assert result[t] == []
