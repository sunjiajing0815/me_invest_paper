"""News fetching service — Alpaca primary, Finnhub fallback, Tavily gap-fill.

Accepts credentials as parameters so the module is pure/testable and does not
read from DB or global config directly.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import ceil
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse, urlunparse

from sqlalchemy.orm import Session

from ..models import NewsEvent

if TYPE_CHECKING:
    from ..graphs.news_triage import NewsTriageItem  # avoid circular import at runtime
    from .tavily import TavilyClient

log = logging.getLogger(__name__)

# Crypto tickers need the "<SYM>/USD" form for Alpaca's news API: the bare ticker
# ("BTC") returns a trickle while "BTC/USD" returns the full feed (≈10-30x more). The
# stored/grouped ticker stays the bare symbol so the email + movers grouping are
# unchanged. Extend as the crypto watchlist grows.
_CRYPTO_NEWS_SYMBOLS: dict[str, str] = {
    "BTC": "BTC/USD",
    "ETH": "ETH/USD",
}

# Augment with a Tavily web search only when the structured feeds returned fewer than
# this many articles — fills gaps (e.g. NFLX, which Alpaca has zero coverage for) without
# burning the Tavily monthly cap on tickers that already have rich coverage.
_TAVILY_GAPFILL_THRESHOLD = 3


@dataclass(frozen=True)
class NewsRaw:
    ticker: str
    headline: str
    snippet: str  # first 400 chars of summary/body
    url: str
    url_hash: str  # sha256(normalised_url)[:16]
    published_at: datetime
    # "tavily" items are shown in the movers email but NEVER persisted to news_event
    # (they must not reach the suggestion engine — ADR-0020). See _build_news_events.
    source: Literal["alpaca", "finnhub", "tavily"]


def _alpaca_news_symbol(ticker: str) -> str:
    """Map a crypto ticker to its Alpaca news symbol ('BTC' -> 'BTC/USD'); else passthrough."""
    return _CRYPTO_NEWS_SYMBOLS.get(ticker, ticker)


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
    # symbols is Optional[str] (comma-separated) in this version of alpaca-py.
    # Crypto tickers are queried under their "<SYM>/USD" news symbol (see
    # _CRYPTO_NEWS_SYMBOLS) but stored under the bare ticker.
    request = NewsRequest(symbols=_alpaca_news_symbol(ticker), start=since)
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
# Tavily web-search news (movers email only — never persisted; see ADR-0020)
# ---------------------------------------------------------------------------


def fetch_tavily_news(
    ticker: str,
    since: datetime,
    *,
    tavily: TavilyClient,
    is_crypto: bool = False,
    max_results: int = 5,
) -> list[NewsRaw]:
    """Web-search news for *ticker* via Tavily, source-tagged ``"tavily"``.

    Used to fill gaps the structured feeds miss (e.g. NFLX). These items are shown in the
    movers email but MUST NOT be persisted to news_event — that table feeds the suggestion
    engine and ADR-0020 forbids Tavily reaching it. The ``"tavily"`` source tag lets
    ``_build_news_events`` skip them. Never raises (the Tavily client returns [] on error
    or monthly-cap).
    """
    now = datetime.now(UTC)
    days = max(1, ceil((now - since).total_seconds() / 86400))
    query = f"{ticker} cryptocurrency news" if is_crypto else f"{ticker} stock news"
    results = tavily.search_news(query, days=days, max_results=max_results)

    seen: dict[str, NewsRaw] = {}
    for r in results:
        if not r.url:
            continue
        url_hash = _hash_url(r.url)
        if url_hash in seen:
            continue
        published_at = (
            datetime(
                r.published_date.year, r.published_date.month, r.published_date.day,
                tzinfo=UTC,
            )
            if r.published_date is not None
            else now
        )
        seen[url_hash] = NewsRaw(
            ticker=ticker,
            headline=r.title,
            snippet=(r.content or "")[:400],
            url=r.url,
            url_hash=url_hash,
            published_at=published_at,
            source="tavily",
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
    tavily: TavilyClient | None = None,
) -> dict[str, list[NewsRaw]]:
    """Return news for each ticker: Alpaca primary, Finnhub fallback, Tavily gap-fill.

    Never raises — returns an empty list for a ticker on total failure.
    Deduplicates across sources by url_hash. When ``tavily`` is supplied and the structured
    feeds returned fewer than ``_TAVILY_GAPFILL_THRESHOLD`` articles, augments with a Tavily
    web search (source-tagged ``"tavily"``, never persisted — see ``fetch_tavily_news``).
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

        # Tavily gap-fill: only when the structured feeds are sparse, to conserve the
        # monthly cap and to target real gaps (e.g. NFLX) rather than already-rich tickers.
        if tavily is not None and len(items) < _TAVILY_GAPFILL_THRESHOLD:
            try:
                items = items + fetch_tavily_news(
                    ticker,
                    since,
                    tavily=tavily,
                    is_crypto=ticker in _CRYPTO_NEWS_SYMBOLS,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("tavily news failed for %s: %s", ticker, e)

        # Final dedup by url_hash across sources (in case two returned the same article)
        seen: dict[str, NewsRaw] = {}
        for item in items:
            if item.url_hash not in seen:
                seen[item.url_hash] = item

        out[ticker] = list(seen.values())

    return out


# ---------------------------------------------------------------------------
# DB query helpers
# ---------------------------------------------------------------------------


def load_recent_material_news(
    session: Session,
    days: int = 7,
) -> dict[str, list[NewsTriageItem]]:
    """Return material news from the last *days* days, grouped by ticker.

    Each entry is a NewsTriageItem (Pydantic BaseModel). All conversion
    from ORM rows happens inside this function — no ORM objects leave.
    """
    # Local import to avoid circular dependency: news_triage.py imports NewsRaw from this module
    from ..graphs.news_triage import NewsTriageItem  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = (
        session.query(NewsEvent)
        .filter(NewsEvent.llm_material.is_(True), NewsEvent.published_at >= cutoff)
        .order_by(NewsEvent.published_at.desc())
        .all()
    )

    result: dict[str, list[NewsTriageItem]] = {}
    for row in rows:
        item = NewsTriageItem(
            url_hash=row.url_hash,
            is_material=True,
            sentiment=row.llm_sentiment,  # type: ignore[arg-type]  # DB stores validated values
            summary=row.llm_summary,
        )
        result.setdefault(row.ticker, []).append(item)

    return result
