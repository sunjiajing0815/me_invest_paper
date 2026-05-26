"""SentimentClient protocol — VIX (Finnhub) and Fear & Greed (CNN) fetching."""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)


@dataclass
class SentimentData:
    vix: float | None = None
    fear_greed_score: int | None = None
    fear_greed_label: str | None = None


class SentimentClient(Protocol):
    def get_sentiment(self) -> SentimentData: ...


class FinnhubCNNSentimentClient:
    """Fetches VIX from Finnhub and Fear & Greed from CNN (no key required for CNN)."""

    def __init__(self, finnhub_api_key: str) -> None:
        self._api_key = finnhub_api_key

    def get_sentiment(self) -> SentimentData:
        vix = self._fetch_vix()
        fear_greed_score, fear_greed_label = self._fetch_fear_greed()
        if vix is not None:
            log.info("SentimentClient: VIX=%.2f", vix)
        if fear_greed_score is not None:
            log.info(
                "SentimentClient: Fear&Greed=%d (%s)", fear_greed_score, fear_greed_label
            )
        return SentimentData(
            vix=vix,
            fear_greed_score=fear_greed_score,
            fear_greed_label=fear_greed_label,
        )

    def _fetch_vix(self) -> float | None:
        if not self._api_key:
            return None
        try:
            import finnhub  # lazy import — already a project dep

            q = finnhub.Client(api_key=self._api_key).quote("^VIX")
            c = q.get("c") if q else None
            return float(c) if c else None
        except Exception as exc:  # noqa: BLE001
            log.warning("SentimentClient._fetch_vix: %s", exc)
            return None

    def _fetch_fear_greed(self) -> tuple[int | None, str | None]:
        try:
            req = urllib.request.Request(
                "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                headers={"User-Agent": "investor-assistant/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            fg = data.get("fear_and_greed", {})
            score = fg.get("score")
            label = fg.get("rating") or None
            return (int(float(score)) if score is not None else None, label)
        except Exception as exc:  # noqa: BLE001
            log.warning("SentimentClient._fetch_fear_greed: %s", exc)
            return None, None


class FakeSentimentClient:
    """Returns canned SentimentData — use in tests."""

    def __init__(self, canned: SentimentData | None = None) -> None:
        self._canned = canned or SentimentData()

    def get_sentiment(self) -> SentimentData:
        return self._canned


def make_sentiment_client(settings: Settings) -> SentimentClient:
    """Factory: always returns FinnhubCNNSentimentClient.

    When finnhub_api_key is empty, VIX will return None but CNN Fear & Greed
    still works (no key required). FakeSentimentClient is for tests only.
    """
    if not settings.finnhub_api_key:
        log.warning(
            "make_sentiment_client: FINNHUB_API_KEY not set — "
            "VIX unavailable; Fear & Greed will still be fetched (no key needed)."
        )
    return FinnhubCNNSentimentClient(finnhub_api_key=settings.finnhub_api_key)
