"""Tests for services/sentiment.py."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from investor.services.sentiment import (
    FakeSentimentClient,
    FinnhubCNNSentimentClient,
    SentimentData,
    make_sentiment_client,
)


class _FakeResp:
    """Minimal context-manager stand-in for urllib.request.urlopen()."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _cnn_payload() -> bytes:
    """A CNN graphdata response carrying both Fear & Greed and VIX."""
    return json.dumps(
        {
            "fear_and_greed": {"score": 42.0571, "rating": "fear"},
            "market_volatility_vix": {"data": [{"x": 1, "y": 20.0}, {"x": 2, "y": 21.51}]},
        }
    ).encode()


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


def test_make_factory_no_key_returns_concrete() -> None:
    settings = SimpleNamespace(finnhub_api_key="")
    client = make_sentiment_client(settings)
    assert isinstance(client, FinnhubCNNSentimentClient)


def test_make_factory_with_key_returns_concrete() -> None:
    settings = SimpleNamespace(finnhub_api_key="testkey")
    client = make_sentiment_client(settings)
    assert isinstance(client, FinnhubCNNSentimentClient)


def test_fetch_cnn_parses_score_and_vix() -> None:
    """_fetch_cnn extracts F&G score+rating and the latest VIX from one payload."""
    client = FinnhubCNNSentimentClient(finnhub_api_key="")
    with patch("urllib.request.urlopen", return_value=_FakeResp(_cnn_payload())):
        score, label, vix = client._fetch_cnn()
    assert score == 42
    assert label == "fear"
    assert vix == 21.51


def test_fetch_cnn_exception_returns_none() -> None:
    """CNN endpoint failure returns (None, None, None)."""
    client = FinnhubCNNSentimentClient(finnhub_api_key="")
    with patch("urllib.request.urlopen", side_effect=OSError("network error")):
        assert client._fetch_cnn() == (None, None, None)


def test_fetch_cnn_malformed_vix_keeps_fear_greed() -> None:
    """A malformed market_volatility_vix section must not drop the F&G result."""
    payload = json.dumps(
        {
            "fear_and_greed": {"score": 55.0, "rating": "greed"},
            "market_volatility_vix": {"data": []},
        }
    ).encode()
    client = FinnhubCNNSentimentClient(finnhub_api_key="")
    with patch("urllib.request.urlopen", return_value=_FakeResp(payload)):
        score, label, vix = client._fetch_cnn()
    assert (score, label) == (55, "greed")
    assert vix is None


def test_vix_fetch_no_key_returns_none() -> None:
    client = FinnhubCNNSentimentClient(finnhub_api_key="")
    assert client._fetch_vix() is None


def test_get_sentiment_uses_cnn_for_both() -> None:
    """get_sentiment surfaces CNN's VIX + F&G without needing Finnhub."""
    client = FinnhubCNNSentimentClient(finnhub_api_key="")
    with patch("urllib.request.urlopen", return_value=_FakeResp(_cnn_payload())):
        sd = client.get_sentiment()
    assert sd.fear_greed_score == 42
    assert sd.fear_greed_label == "fear"
    assert sd.vix == 21.51


def test_get_sentiment_falls_back_to_finnhub_vix() -> None:
    """When CNN has no VIX, get_sentiment falls back to Finnhub for VIX."""
    client = FinnhubCNNSentimentClient(finnhub_api_key="testkey")

    def _cnn() -> tuple[int | None, str | None, float | None]:
        return (42, "Fear", None)

    def _vix() -> float | None:
        return 19.0

    client._fetch_cnn = _cnn  # type: ignore[method-assign]
    client._fetch_vix = _vix  # type: ignore[method-assign]
    sd = client.get_sentiment()
    assert sd.vix == 19.0
    assert sd.fear_greed_score == 42
    assert sd.fear_greed_label == "Fear"


def test_vix_fallback_exception_returns_none() -> None:
    """Finnhub raising during the VIX fallback yields vix=None (CNN had no VIX)."""
    client = FinnhubCNNSentimentClient(finnhub_api_key="testkey")

    def _cnn() -> tuple[int | None, str | None, float | None]:
        return (None, None, None)

    client._fetch_cnn = _cnn  # type: ignore[method-assign]
    with patch("finnhub.Client") as mock_cls:
        mock_cls.return_value.quote.side_effect = RuntimeError("API error")
        sd = client.get_sentiment()
    assert sd.vix is None
