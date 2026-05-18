"""Tests for src/investor/jobs/weekly_suggestions.py."""

from __future__ import annotations

import json
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
            result, failures = score_all_tickers_parallel(
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
        assert failures == []

    def test_parallel_scoring_failure_fallback(self, caplog: object) -> None:
        """One ticker raises → gets [] in result; others succeed normally."""
        import logging

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

            with caplog.at_level(logging.ERROR, logger="investor.jobs.weekly_suggestions"):
                result, failures = score_all_tickers_parallel(
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
        # Failure is recorded
        assert len(failures) == 1
        assert failures[0].ticker == failing_ticker
        assert failures[0].exc_type == "RuntimeError"
        # RuntimeError (non-JSON) must emit an ERROR-level log record
        assert any(
            r.levelname == "ERROR" and failing_ticker in r.message
            for r in caplog.records
        )

    def test_scoring_failure_surfaced(self, caplog: object) -> None:
        """JSONDecodeError from LLM scoring is captured in failures list."""
        import logging

        tickers = _make_tickers(3)
        failing_ticker = tickers[0]  # "AAPL" raises JSONDecodeError
        mock_llm = MagicMock()

        def _maybe_raise(*args: object, **kwargs: object) -> list[ScoredLevel]:
            ticker_arg = kwargs.get("ticker", "")
            if ticker_arg == failing_ticker:
                raise json.JSONDecodeError("Expecting value", "bad json", 0)
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

            with caplog.at_level(logging.WARNING, logger="investor.jobs.weekly_suggestions"):
                result, failures = score_all_tickers_parallel(
                    tickers=tickers,
                    sr_rows=[],
                    llm=mock_llm,
                    bars_dir="data/bars",
                    recent_news_by_ticker={},
                    prompt_version="v2",
                    max_workers=4,
                )

        assert len(failures) == 1
        assert failures[0].ticker == failing_ticker
        assert failures[0].exc_type == "JSONDecodeError"
        assert result[failing_ticker] == []
        # exc_message is populated and truncated to ≤ 200 chars
        assert failures[0].exc_message != ""
        assert len(failures[0].exc_message) <= 200
        # Non-failing tickers are present and succeed
        for t in tickers:
            if t != failing_ticker:
                assert t in result
        # JSON/Validation errors must emit a WARNING-level log record for the failing ticker
        assert any(
            r.levelname == "WARNING" and failing_ticker in r.message
            for r in caplog.records
        )
