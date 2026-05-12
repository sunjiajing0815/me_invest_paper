"""News fetching service — Alpaca primary, Finnhub fallback.

Accepts credentials as parameters so the module is pure/testable and does not
read from DB or global config directly.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsRaw:
    ticker: str
    headline: str
    snippet: str  # first 400 chars of summary/body
    url: str
    url_hash: str  # sha256(normalised_url)[:16]
    published_at: datetime
    source: Literal["alpaca", "finnhub"]


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------


def _normalise_url(url: str) -> str:
    """Strip query params and lowercase host.

    Prevents Alpaca+Finnhub dedup failures when the same story is served
    under slightly different query strings.
    """
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, "", "", ""))


def _hash_url(url: str) -> str:
    return hashlib.sha256(_normalise_url(url).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Alpaca news
# ---------------------------------------------------------------------------


def fetch_alpaca_news(
    ticker: str,
    since: datetime,
    *,
    api_key: str,
    secret_key: str,
) -> list[NewsRaw]:
    """Fetch recent news for *ticker* from Alpaca, starting at *since*.

    Returns a deduplicated list of :class:`NewsRaw` items.
    Raises on Alpaca client errors — callers should catch.
    """
    from alpaca.data.historical import NewsClient
    from alpaca.data.requests import NewsRequest

    client: Any = NewsClient(api_key=api_key, secret_key=secret_key)
    # symbols is Optional[str] (comma-separated) in this version of alpaca-py
    request = NewsRequest(symbols=ticker, start=since)
    result: Any = client.get_news(request)

    # NewsSet.data is keyed by "news" (flat list, not per-ticker) in this SDK version
    raw_articles: Any = result.data.get("news", [])

    seen: dict[str, NewsRaw] = {}
    for a in raw_articles:
        article_url: str = a.url or ""
        if not article_url:
            continue
        url_hash = _hash_url(article_url)
        if url_hash not in seen:
            seen[url_hash] = NewsRaw(
                ticker=ticker,
                headline=str(a.headline),
                snippet=(a.summary or "")[:400],
                url=article_url,
                url_hash=url_hash,
                published_at=a.created_at,
                source="alpaca",
            )

    return list(seen.values())


# ---------------------------------------------------------------------------
# Finnhub news
# ---------------------------------------------------------------------------


def fetch_finnhub_news(
    ticker: str,
    since: datetime,
    *,
    finnhub_api_key: str,
) -> list[NewsRaw]:
    """Fetch recent news for *ticker* from Finnhub, starting at *since*.

    Returns a deduplicated list of :class:`NewsRaw` items filtered to
    ``published_at >= since``.
    Raises on Finnhub client errors — callers should catch.
    """
    import finnhub

    client: Any = finnhub.Client(api_key=finnhub_api_key)
    articles: list[dict[str, Any]] = client.company_news(
        ticker,
        _from=since.strftime("%Y-%m-%d"),
        to=date.today().strftime("%Y-%m-%d"),
    )

    seen: dict[str, NewsRaw] = {}
    for a in articles:
        raw_url = str(a.get("url") or "")
        if not raw_url:
            continue

        ts = a.get("datetime")
        if ts is None:
            continue
        published_at = datetime.fromtimestamp(float(ts), tz=UTC)
        if published_at < since:
            continue

        url_hash = _hash_url(raw_url)
        if url_hash not in seen:
            seen[url_hash] = NewsRaw(
                ticker=ticker,
                headline=str(a.get("headline") or ""),
                snippet=(str(a.get("summary") or ""))[:400],
                url=raw_url,
                url_hash=url_hash,
                published_at=published_at,
                source="finnhub",
            )

    return list(seen.values())


# ---------------------------------------------------------------------------
# Aggregated entry-point
# ---------------------------------------------------------------------------


def get_news_for_movers(
    tickers: list[str],
    since: datetime,
    *,
    alpaca_api_key: str,
    alpaca_secret_key: str,
    finnhub_api_key: str,
) -> dict[str, list[NewsRaw]]:
    """Return news for each ticker: Alpaca primary, Finnhub fallback.

    Never raises — returns an empty list for a ticker on total failure.
    Deduplicates across sources by url_hash.
    """
    out: dict[str, list[NewsRaw]] = {}

    for ticker in tickers:
        items: list[NewsRaw] = []

        try:
            items = fetch_alpaca_news(
                ticker,
                since,
                api_key=alpaca_api_key,
                secret_key=alpaca_secret_key,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("alpaca news failed for %s: %s", ticker, e)

        if not items:
            try:
                items = fetch_finnhub_news(
                    ticker,
                    since,
                    finnhub_api_key=finnhub_api_key,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("finnhub news failed for %s: %s", ticker, e)

        # Final dedup by url_hash across sources (in case both returned same article)
        seen: dict[str, NewsRaw] = {}
        for item in items:
            if item.url_hash not in seen:
                seen[item.url_hash] = item

        out[ticker] = list(seen.values())

    return out
