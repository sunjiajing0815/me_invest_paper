"""SentimentClient protocol — VIX + Fear & Greed from CNN's graphdata payload.

CNN's dataviz endpoint serves both the Fear & Greed score and the latest VIX in a
single JSON document, so we read both from there. Finnhub is kept only as a VIX
fallback (its free tier returns no data for the ``^VIX`` index symbol).
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)

_CNN_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# CNN's dataviz endpoint returns HTTP 418 to bot User-Agents; a browser-like header
# set (UA + Accept + Origin/Referer pointing at cnn.com) is required for a 200.
_CNN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.cnn.com",
    "Referer": "https://www.cnn.com/",
}


@dataclass
class SentimentData:
    vix: float | None = None
    fear_greed_score: int | None = None
    fear_greed_label: str | None = None


class SentimentClient(Protocol):
    def get_sentiment(self) -> SentimentData: ...


class FinnhubCNNSentimentClient:
    """Fetches Fear & Greed and VIX from CNN; Finnhub is a VIX fallback only."""

    def __init__(self, finnhub_api_key: str) -> None:
        self._api_key = finnhub_api_key

    def get_sentiment(self) -> SentimentData:
        fear_greed_score, fear_greed_label, vix = self._fetch_cnn()
        if vix is None:
            vix = self._fetch_vix()  # Finnhub fallback (free tier serves no ^VIX data)
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

    def _fetch_cnn(self) -> tuple[int | None, str | None, float | None]:
        """Return (fear_greed_score, fear_greed_label, vix) from CNN's graphdata.

        Both values come from one payload: ``fear_and_greed`` and the latest point of
        ``market_volatility_vix``. A malformed VIX section does not drop the F&G result.
        """
        try:
            req = urllib.request.Request(_CNN_FEAR_GREED_URL, headers=_CNN_HEADERS)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data: dict[str, Any] = json.loads(resp.read())
            fg = data.get("fear_and_greed", {})
            score = fg.get("score")
            score_int = int(float(score)) if score is not None else None
            label = fg.get("rating") or None
            try:
                points = data.get("market_volatility_vix", {}).get("data") or []
                vix = float(points[-1]["y"]) if points else None
            except (KeyError, IndexError, TypeError, ValueError):
                vix = None
            return (score_int, label, vix)
        except Exception as exc:  # noqa: BLE001
            log.warning("SentimentClient._fetch_cnn: %s", exc)
            return None, None, None

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


class FakeSentimentClient:
    """Returns canned SentimentData — use in tests."""

    def __init__(self, canned: SentimentData | None = None) -> None:
        self._canned = canned or SentimentData()

    def get_sentiment(self) -> SentimentData:
        return self._canned


def make_sentiment_client(settings: Settings) -> SentimentClient:
    """Factory: always returns FinnhubCNNSentimentClient.

    Both VIX and Fear & Greed come from CNN (no key required); the Finnhub key is only
    a VIX fallback. FakeSentimentClient is for tests only.
    """
    return FinnhubCNNSentimentClient(finnhub_api_key=settings.finnhub_api_key)
