"""Tests for services/tavily.py: FakeTavilyClient, TavilyConcreteClient, factory."""
from __future__ import annotations

import logging
from datetime import date
from unittest.mock import MagicMock, patch

from investor.services.tavily import (
    FakeTavilyClient,
    NewsResult,
    TavilyConcreteClient,
    make_tavily_client,
)


def _news(
    title: str = "Headline",
    url: str = "https://example.com/a",
    score: float = 0.8,
) -> NewsResult:
    return NewsResult(
        title=title,
        url=url,
        content="Some content",
        published_date=date(2026, 5, 12),
        source_domain="example.com",
        score=score,
    )


class TestFakeTavilyClient:
    def test_records_search_news_call(self):
        client = FakeTavilyClient()
        client.search_news("Fed policy", days=7)
        assert client.calls == [("Fed policy", "news", 7)]

    def test_records_search_finance_call(self):
        client = FakeTavilyClient()
        client.search_finance("AAPL news", days=3)
        assert client.calls == [("AAPL news", "finance", 3)]

    def test_records_multiple_calls_in_order(self):
        client = FakeTavilyClient()
        client.search_news("q1", days=7)
        client.search_finance("q2", days=3)
        assert client.calls == [("q1", "news", 7), ("q2", "finance", 3)]

    def test_returns_canned_result(self):
        r = _news()
        client = FakeTavilyClient(canned={"Fed policy": [r]})
        assert client.search_news("Fed policy") == [r]

    def test_returns_empty_for_unknown_query(self):
        client = FakeTavilyClient(canned={"other": [_news()]})
        assert client.search_news("unknown query") == []

    def test_empty_canned_by_default(self):
        client = FakeTavilyClient()
        assert client.search_finance("anything") == []


class TestMakeTavilyClient:
    def _settings(self, key: str = "", cap: int = 200):
        class _S:
            tavily_api_key = key
            tavily_monthly_cap = cap
        return _S()

    def test_no_key_returns_fake_client(self):
        client = make_tavily_client(self._settings(key=""))
        assert isinstance(client, FakeTavilyClient)

    def test_with_key_returns_concrete_client(self):
        mock_tavily = MagicMock()
        with patch.dict("sys.modules", {"tavily": mock_tavily}):
            client = make_tavily_client(self._settings(key="sk-test"))
        assert isinstance(client, TavilyConcreteClient)

    def test_no_key_logs_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="investor.services.tavily"):
            make_tavily_client(self._settings(key=""))
        assert "TAVILY_API_KEY" in caplog.text


class TestTavilyConcreteClient:
    def _make_client(self, cap: int = 10) -> TavilyConcreteClient:
        mock_tavily = MagicMock()
        with patch.dict("sys.modules", {"tavily": mock_tavily}):
            client = TavilyConcreteClient(api_key="test-key", monthly_searches_cap=cap)
        return client

    def test_cap_enforced_returns_empty(self):
        client = self._make_client(cap=5)
        client._used_this_month = 5  # at cap
        assert client.search_news("any query") == []

    def test_cap_enforced_logs_warning(self, caplog):
        client = self._make_client(cap=3)
        client._used_this_month = 3
        with caplog.at_level(logging.WARNING, logger="investor.services.tavily"):
            client.search_news("any query")
        assert "cap" in caplog.text.lower()

    def test_sdk_exception_returns_empty(self):
        client = self._make_client()
        client._client.search.side_effect = RuntimeError("network error")
        assert client.search_news("any query") == []

    def test_sdk_exception_logs_warning(self, caplog):
        client = self._make_client()
        client._client.search.side_effect = ValueError("bad response")
        with caplog.at_level(logging.WARNING, logger="investor.services.tavily"):
            client.search_news("any query")
        assert "Tavily search failed" in caplog.text

    def test_success_increments_counter(self):
        client = self._make_client()
        client._client.search.return_value = {
            "results": [
                {"title": "T", "url": "https://ex.com/x", "content": "c", "score": 0.9}
            ]
        }
        client.search_news("query")
        assert client._used_this_month == 1

    def test_maps_result_to_news_result_fields(self):
        client = self._make_client()
        client._client.search.return_value = {
            "results": [
                {
                    "title": "Fed raises rates",
                    "url": "https://wsj.com/article",
                    "content": "The Federal Reserve raised rates by 25bps.",
                    "score": 0.95,
                    "published_date": "2026-05-14",
                }
            ]
        }
        results = client.search_news("Fed policy")
        assert len(results) == 1
        r = results[0]
        assert r.title == "Fed raises rates"
        assert r.url == "https://wsj.com/article"
        assert r.score == 0.95
        assert r.published_date == date(2026, 5, 14)
        assert r.source_domain == "wsj.com"

    def test_content_capped_at_1500_chars(self):
        client = self._make_client()
        client._client.search.return_value = {
            "results": [
                {"title": "T", "url": "https://ex.com/x", "content": "x" * 2000, "score": 0.5}
            ]
        }
        results = client.search_news("query")
        assert len(results[0].content) == 1500
