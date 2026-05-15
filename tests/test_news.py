"""Tests for src/investor/services/news.py and tiered threshold logic in jobs/movers.py."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from investor.services.news import (
    NewsRaw,
    _hash_url,
    _normalise_url,
    fetch_alpaca_news,
    fetch_finnhub_news,
    get_news_for_movers,
)

# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------


class TestNormaliseUrl:
    def test_normalise_url_strips_query_params(self) -> None:
        assert _normalise_url("https://x.com/a?utm=1") == "https://x.com/a"

    def test_normalise_url_lowercases_host(self) -> None:
        assert _normalise_url("HTTPS://X.COM/a") == "https://x.com/a"


class TestHashUrl:
    def test_hash_url_same_after_normalisation(self) -> None:
        url1 = "https://example.com/article?utm_source=twitter&utm_medium=social"
        url2 = "https://example.com/article?utm_source=email"
        assert _hash_url(url1) == _hash_url(url2)

    def test_hash_url_returns_16_chars(self) -> None:
        h = _hash_url("https://example.com/article")
        assert len(h) == 16

    def test_hash_url_matches_manual_computation(self) -> None:
        url = "https://example.com/article"
        expected = hashlib.sha256(url.encode()).hexdigest()[:16]
        assert _hash_url(url) == expected


# ---------------------------------------------------------------------------
# fetch_alpaca_news mock
# ---------------------------------------------------------------------------


class TestFetchAlpacaNews:
    def _make_article(
        self,
        url: str = "https://news.alpaca.com/story1",
        headline: str = "Apple Rises",
        summary: str = "Apple stock up 5% today",
        created_at: datetime | None = None,
    ) -> MagicMock:
        article = MagicMock()
        article.url = url
        article.headline = headline
        article.summary = summary
        article.created_at = created_at or datetime(2024, 1, 10, 12, 0, tzinfo=UTC)
        return article

    def _patch_alpaca(
        self,
        articles: list,
    ):
        """Return a context manager that patches alpaca-py's NewsClient inside fetch_alpaca_news."""
        mock_news_client_cls = MagicMock()
        mock_client_instance = MagicMock()
        mock_news_client_cls.return_value = mock_client_instance
        mock_result = MagicMock()
        mock_result.data = {"news": articles}
        mock_client_instance.get_news.return_value = mock_result

        # The imports happen inside fetch_alpaca_news, so patch the alpaca sub-modules directly
        import unittest.mock as _mock
        return _mock.patch.multiple(
            "alpaca.data.historical",
            NewsClient=mock_news_client_cls,
        ), _mock.patch("alpaca.data.requests.NewsRequest", MagicMock())

    def test_fetch_alpaca_news_mock(self) -> None:
        article = self._make_article()
        since = datetime(2024, 1, 9, tzinfo=UTC)

        mock_news_client_cls = MagicMock()
        mock_client_instance = MagicMock()
        mock_news_client_cls.return_value = mock_client_instance
        mock_result = MagicMock()
        mock_result.data = {"news": [article]}
        mock_client_instance.get_news.return_value = mock_result

        with (
            patch("alpaca.data.historical.NewsClient", mock_news_client_cls),
            patch("alpaca.data.requests.NewsRequest", MagicMock()),
        ):
            results = fetch_alpaca_news(
                "AAPL",
                since,
                api_key="test-key",
                secret_key="test-secret",
            )

        assert len(results) == 1
        item = results[0]
        assert isinstance(item, NewsRaw)
        assert item.ticker == "AAPL"
        assert item.headline == "Apple Rises"
        assert item.snippet == "Apple stock up 5% today"
        assert item.url == "https://news.alpaca.com/story1"
        assert item.source == "alpaca"
        assert len(item.url_hash) == 16
        assert item.published_at == datetime(2024, 1, 10, 12, 0, tzinfo=UTC)

    def test_fetch_alpaca_news_deduplicates_same_url(self) -> None:
        url = "https://news.alpaca.com/story1"
        article1 = self._make_article(url=url)
        article2 = self._make_article(url=url, headline="Duplicate Headline")
        mock_news_client_cls = MagicMock()
        mock_client_instance = MagicMock()
        mock_news_client_cls.return_value = mock_client_instance
        mock_result = MagicMock()
        mock_result.data = {"news": [article1, article2]}
        mock_client_instance.get_news.return_value = mock_result

        with (
            patch("alpaca.data.historical.NewsClient", mock_news_client_cls),
            patch("alpaca.data.requests.NewsRequest", MagicMock()),
        ):
            results = fetch_alpaca_news(
                "AAPL",
                datetime(2024, 1, 9, tzinfo=UTC),
                api_key="k",
                secret_key="s",
            )

        assert len(results) == 1

    def test_fetch_alpaca_news_skips_empty_url(self) -> None:
        article = self._make_article(url="")
        mock_news_client_cls = MagicMock()
        mock_client_instance = MagicMock()
        mock_news_client_cls.return_value = mock_client_instance
        mock_result = MagicMock()
        mock_result.data = {"news": [article]}
        mock_client_instance.get_news.return_value = mock_result

        with (
            patch("alpaca.data.historical.NewsClient", mock_news_client_cls),
            patch("alpaca.data.requests.NewsRequest", MagicMock()),
        ):
            results = fetch_alpaca_news(
                "AAPL",
                datetime(2024, 1, 9, tzinfo=UTC),
                api_key="k",
                secret_key="s",
            )

        assert results == []


# ---------------------------------------------------------------------------
# fetch_finnhub_news mock
# ---------------------------------------------------------------------------


class TestFetchFinnhubNews:
    def _make_article(
        self,
        url: str = "https://finnhub.io/news/article1",
        headline: str = "Tesla Up",
        summary: str = "Tesla stock rallies",
        ts: int = 1704844800,  # 2024-01-10 00:00:00 UTC
    ) -> dict:
        return {
            "url": url,
            "headline": headline,
            "summary": summary,
            "datetime": ts,
        }

    def test_fetch_finnhub_news_mock(self) -> None:
        article = self._make_article()
        mock_client_instance = MagicMock()
        mock_client_instance.company_news.return_value = [article]

        since = datetime(2024, 1, 9, tzinfo=UTC)

        with patch("finnhub.Client", return_value=mock_client_instance):
            results = fetch_finnhub_news(
                "TSLA",
                since,
                finnhub_api_key="test-finnhub-key",
            )

        assert len(results) == 1
        item = results[0]
        assert isinstance(item, NewsRaw)
        assert item.ticker == "TSLA"
        assert item.headline == "Tesla Up"
        assert item.snippet == "Tesla stock rallies"
        assert item.url == "https://finnhub.io/news/article1"
        assert item.source == "finnhub"
        assert len(item.url_hash) == 16

    def test_fetch_finnhub_news_filters_old_articles(self) -> None:
        old_ts = 1704067200  # 2024-01-01 00:00:00 UTC (before 'since')
        article = self._make_article(ts=old_ts)
        mock_client_instance = MagicMock()
        mock_client_instance.company_news.return_value = [article]

        since = datetime(2024, 1, 9, tzinfo=UTC)

        with patch("finnhub.Client", return_value=mock_client_instance):
            results = fetch_finnhub_news("TSLA", since, finnhub_api_key="k")

        assert results == []


# ---------------------------------------------------------------------------
# get_news_for_movers fallback logic
# ---------------------------------------------------------------------------


class TestGetNewsForMovers:
    def _make_raw(
        self,
        ticker: str = "AAPL",
        url: str = "https://news.example.com/article1",
        source: str = "alpaca",
    ) -> NewsRaw:
        return NewsRaw(
            ticker=ticker,
            headline="Test Headline",
            snippet="Test snippet",
            url=url,
            url_hash=_hash_url(url),
            published_at=datetime(2024, 1, 10, 12, 0, tzinfo=UTC),
            source=source,  # type: ignore[arg-type]
        )

    def test_get_news_for_movers_falls_back_to_finnhub(self) -> None:
        """Alpaca raises → Finnhub called. Both fail → empty list, no exception."""
        since = datetime(2024, 1, 9, tzinfo=UTC)
        alpaca_exception = RuntimeError("alpaca down")
        finnhub_exception = RuntimeError("finnhub down")

        with (
            patch("investor.services.news.fetch_alpaca_news", side_effect=alpaca_exception),
            patch("investor.services.news.fetch_finnhub_news", side_effect=finnhub_exception),
        ):
            result = get_news_for_movers(
                ["AAPL"],
                since,
                alpaca_api_key="k",
                alpaca_secret_key="s",
                finnhub_api_key="fk",
            )

        assert result == {"AAPL": []}

    def test_get_news_for_movers_uses_alpaca_primary(self) -> None:
        alpaca_raw = self._make_raw(url="https://alpaca.com/story")
        since = datetime(2024, 1, 9, tzinfo=UTC)

        with (
            patch("investor.services.news.fetch_alpaca_news", return_value=[alpaca_raw]),
            patch("investor.services.news.fetch_finnhub_news") as mock_finnhub,
        ):
            result = get_news_for_movers(
                ["AAPL"],
                since,
                alpaca_api_key="k",
                alpaca_secret_key="s",
                finnhub_api_key="fk",
            )

        # Finnhub not called since Alpaca succeeded
        mock_finnhub.assert_not_called()
        assert len(result["AAPL"]) == 1

    def test_get_news_for_movers_alpaca_fails_uses_finnhub(self) -> None:
        finnhub_raw = self._make_raw(url="https://finnhub.com/story", source="finnhub")
        since = datetime(2024, 1, 9, tzinfo=UTC)

        with (
            patch("investor.services.news.fetch_alpaca_news", side_effect=RuntimeError("fail")),
            patch("investor.services.news.fetch_finnhub_news", return_value=[finnhub_raw]),
        ):
            result = get_news_for_movers(
                ["AAPL"],
                since,
                alpaca_api_key="k",
                alpaca_secret_key="s",
                finnhub_api_key="fk",
            )

        assert len(result["AAPL"]) == 1
        assert result["AAPL"][0].source == "finnhub"

    def test_dedup_by_url_hash(self) -> None:
        """Two NewsRaw with same normalised URL → deduped to one."""
        url = "https://news.example.com/same-story"
        raw1 = NewsRaw(
            ticker="AAPL",
            headline="Story A",
            snippet="snippet",
            url=url,
            url_hash=_hash_url(url),
            published_at=datetime(2024, 1, 10, tzinfo=UTC),
            source="alpaca",
        )
        raw2 = NewsRaw(
            ticker="AAPL",
            headline="Story A (duplicate)",
            snippet="snippet",
            url=url,
            url_hash=_hash_url(url),
            published_at=datetime(2024, 1, 10, tzinfo=UTC),
            source="finnhub",
        )
        since = datetime(2024, 1, 9, tzinfo=UTC)

        # Alpaca returns one raw, then finnhub (fallback won't run since alpaca returned items)
        with (
            patch("investor.services.news.fetch_alpaca_news", return_value=[raw1, raw2]),
        ):
            result = get_news_for_movers(
                ["AAPL"],
                since,
                alpaca_api_key="k",
                alpaca_secret_key="s",
                finnhub_api_key="fk",
            )

        # Both have same url_hash → deduped to 1
        assert len(result["AAPL"]) == 1


# ---------------------------------------------------------------------------
# Tiered threshold logic (tested via run_movers_email)
# ---------------------------------------------------------------------------


def _make_movers_df(ticker: str, pct: float) -> pd.DataFrame:
    return pd.DataFrame([{
        "ticker": ticker,
        "today_close": 110.0,
        "last_week_close": 100.0,
        "pct_change": pct,
    }])


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.bars_dir = "/tmp/fake-bars"
    settings.alpaca_api_key = "key"
    settings.alpaca_secret_key = "secret"
    settings.finnhub_api_key = "fk"
    settings.sqlite_path = ":memory:"
    settings.email_to = "user@example.com"
    return settings


def _run_movers_with_state(
    ticker: str,
    pct: float,
    last_triggered: float,
    has_db_state: bool = True,
) -> list[str]:
    """
    Run run_movers_email with a single ticker at given pct and DB state.
    Returns the list of tickers that ended up in tickers_to_process.
    """
    from investor.jobs.movers import run_movers_email
    from investor.models import MoverState

    df = _make_movers_df(ticker, pct)
    settings = _make_settings()
    adapter = MagicMock()
    emailer = MagicMock()
    llm = MagicMock()

    # Build mock DB state
    if has_db_state and last_triggered > 0.0:
        mock_state = MagicMock(spec=MoverState)
        mock_state.ticker = ticker
        mock_state.last_triggered_threshold = last_triggered
    elif has_db_state and last_triggered == 0.0:
        # State exists but last_triggered=0 means never triggered
        mock_state = MagicMock(spec=MoverState)
        mock_state.ticker = ticker
        mock_state.last_triggered_threshold = 0.0
    else:
        mock_state = None

    db_states = {ticker: mock_state} if mock_state is not None else {}

    processed_tickers: list[str] = []

    def mock_get_news(tickers, since, **kwargs):
        return {t: [] for t in tickers}

    # We capture tickers_to_process by intercepting get_news_for_movers
    original_get_news = mock_get_news

    def capturing_get_news(tickers, since, **kwargs):
        processed_tickers.extend(tickers)
        return original_get_news(tickers, since, **kwargs)

    mock_session = MagicMock()
    mock_session.query.return_value.all.return_value = list(db_states.values())

    from contextlib import contextmanager

    @contextmanager
    def mock_session_scope():
        yield mock_session

    with (
        patch("investor.jobs.movers._compute_movers", return_value=df),
        patch("investor.jobs.movers.session_scope", mock_session_scope),
        patch("investor.jobs.movers.get_news_for_movers", side_effect=capturing_get_news),
        patch("investor.jobs.movers.build_news_triage_graph", return_value=MagicMock()),
        patch("investor.jobs.movers.render_template", return_value="<html/>"),
    ):
        run_movers_email(settings, adapter, emailer, llm)

    return processed_tickers


def _run_movers_check_resets(
    ticker: str,
    pct: float,
    last_triggered: float,
) -> list[str]:
    """Run run_movers_email and return the list of tickers_to_reset."""
    from investor.jobs.movers import run_movers_email
    from investor.models import MoverState

    df = _make_movers_df(ticker, pct)
    settings = _make_settings()
    adapter = MagicMock()
    emailer = MagicMock()
    llm = MagicMock()

    mock_state = MagicMock(spec=MoverState)
    mock_state.ticker = ticker
    mock_state.last_triggered_threshold = last_triggered

    # First session: for loading state (step 2)
    # Second session: for resetting (step: tickers_to_reset)
    mock_session_load = MagicMock()
    mock_session_load.query.return_value.all.return_value = [mock_state]

    reset_calls: list[str] = []

    mock_session_reset = MagicMock()

    def capturing_merge_reset(obj):
        if hasattr(obj, "last_triggered_threshold") and obj.last_triggered_threshold == 0.0:
            reset_calls.append(obj.ticker)

    mock_session_reset.merge.side_effect = capturing_merge_reset

    import contextlib
    from contextlib import contextmanager

    call_count = 0

    @contextmanager
    def mock_session_scope():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield mock_session_load
        else:
            yield mock_session_reset

    with (
        patch("investor.jobs.movers._compute_movers", return_value=df),
        patch("investor.jobs.movers.session_scope", mock_session_scope),
        patch("investor.jobs.movers.get_news_for_movers", return_value={ticker: []}),
        patch("investor.jobs.movers.build_news_triage_graph", return_value=MagicMock()),
        patch("investor.jobs.movers.render_template", return_value="<html/>"),
        contextlib.suppress(Exception),
    ):
        run_movers_email(settings, adapter, emailer, llm)

    return reset_calls


class TestTieredThreshold:
    def test_tiered_threshold_first_crossing(self) -> None:
        """Ticker at 6%, no prior state → next_threshold=5.0, crosses → included."""
        result = _run_movers_with_state("AAPL", pct=6.0, last_triggered=0.0, has_db_state=False)
        assert "AAPL" in result

    def test_tiered_threshold_no_second_crossing(self) -> None:
        """Ticker at 6%, last_triggered=5.0 → next_threshold=10.0 → NOT included."""
        result = _run_movers_with_state("AAPL", pct=6.0, last_triggered=5.0, has_db_state=True)
        assert "AAPL" not in result

    def test_tiered_threshold_escalation(self) -> None:
        """Ticker at 11%, last_triggered=5.0 → next_threshold=10.0, crosses → included."""
        result = _run_movers_with_state("AAPL", pct=11.0, last_triggered=5.0, has_db_state=True)
        assert "AAPL" in result

    def test_tiered_threshold_reset(self) -> None:
        """Ticker at 2%, last_triggered=5.0 → abs(pct) < 5.0 → added to tickers_to_reset."""
        reset_tickers = _run_movers_check_resets("AAPL", pct=2.0, last_triggered=5.0)
        assert "AAPL" in reset_tickers

    def test_tiered_threshold_retrigger_after_reset(self) -> None:
        """Ticker previously reset to 0.0, then moves to 6% → next_threshold=5.0, crosses."""
        # last_triggered=0.0 means reset; state exists but threshold=0
        result = _run_movers_with_state("AAPL", pct=6.0, last_triggered=0.0, has_db_state=True)
        assert "AAPL" in result

    def test_tiered_threshold_negative_pct_first_crossing(self) -> None:
        """Ticker at -6% (down), no prior state → abs(pct)=6 >= 5.0 → included."""
        result = _run_movers_with_state("AAPL", pct=-6.0, last_triggered=0.0, has_db_state=False)
        assert "AAPL" in result

    def test_tiered_threshold_below_5_no_state_not_included(self) -> None:
        """Ticker at 3%, no prior state → abs(pct) < 5.0 → not included, not reset."""
        result = _run_movers_with_state("AAPL", pct=3.0, last_triggered=0.0, has_db_state=False)
        assert "AAPL" not in result
