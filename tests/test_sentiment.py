"""Tests for services/sentiment.py."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from investor.services.sentiment import (
    FakeSentimentClient,
    FinnhubCNNSentimentClient,
    SentimentData,
    make_sentiment_client,
)


def test_fake_returns_canned_data() -> None:
    canned = SentimentData(vix=18.5, fear_greed_score=25, fear_greed_label="Extreme Fear")
    c = FakeSentimentClient(canned=canned)
    assert c.get_sentiment() == canned


def test_fake_returns_none_defaults() -> None:
    c = FakeSentimentClient()
    sd = c.get_sentiment()
    assert sd.vix is None
    assert sd.fear_greed_score is None
    assert sd.fear_greed_label is None


def test_make_factory_no_key_returns_concrete_with_no_vix() -> None:
    """Without finnhub key, factory returns FinnhubCNNSentimentClient (VIX will be None)."""
    settings = SimpleNamespace(finnhub_api_key="")
    client = make_sentiment_client(settings)
    assert isinstance(client, FinnhubCNNSentimentClient)


def test_make_factory_with_key_returns_concrete() -> None:
    settings = SimpleNamespace(finnhub_api_key="testkey")
    client = make_sentiment_client(settings)
    assert isinstance(client, FinnhubCNNSentimentClient)


def test_vix_fetch_no_key_returns_none() -> None:
    client = FinnhubCNNSentimentClient(finnhub_api_key="")
    assert client._fetch_vix() is None


def test_vix_fetch_exception_returns_none() -> None:
    """Finnhub raising an exception returns None for VIX."""
    client = FinnhubCNNSentimentClient(finnhub_api_key="testkey")
    with patch("finnhub.Client") as mock_cls:
        mock_cls.return_value.quote.side_effect = RuntimeError("API error")
        sd = client.get_sentiment()
    assert sd.vix is None


def test_fear_greed_exception_returns_none() -> None:
    """CNN endpoint failure returns (None, None) for Fear & Greed."""
    client = FinnhubCNNSentimentClient(finnhub_api_key="")
    with patch("urllib.request.urlopen", side_effect=OSError("network error")):
        score, label = client._fetch_fear_greed()
    assert score is None
    assert label is None


def test_get_sentiment_combines_results() -> None:
    """get_sentiment aggregates vix + fear_greed into SentimentData."""
    client = FinnhubCNNSentimentClient(finnhub_api_key="")

    def _mock_fear_greed():
        return (42, "Fear")

    client._fetch_fear_greed = _mock_fear_greed  # type: ignore[method-assign]
    sd = client.get_sentiment()
    # VIX is None because api_key is empty
    assert sd.vix is None
    assert sd.fear_greed_score == 42
    assert sd.fear_greed_label == "Fear"
