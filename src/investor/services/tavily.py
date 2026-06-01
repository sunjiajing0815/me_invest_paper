"""Tavily search API wrapper — thin Protocol-backed client for weekly market context.

TavilyClient is a Protocol so the concrete implementation can be swapped
(Serper, Brave, Perplexity) if Tavily becomes unsuitable post-Nebius acquisition.
See ADR-0020.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsResult:
    """Single search result from Tavily, projected to a plain frozen dataclass."""

    title: str
    url: str
    content: str          # extracted snippet, capped at 1500 chars
    published_date: date | None
    source_domain: str
    score: float          # Tavily relevance score 0..1


def _domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc
        return netloc.removeprefix("www.") or url
    except Exception:  # noqa: BLE001
        return url


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _days_to_time_range(days: int) -> str:
    """Map a 'days back' window to Tavily's ``time_range`` bucket.

    Tavily honors ``days`` only for ``topic="news"``; for ``topic="finance"`` (and
    "general") it is ignored, so a finance search returns its most *relevant* match
    regardless of date — which surfaced a stale "BTC hit $73k" article in a later
    week's review. ``time_range`` is honored for ALL topics, so we always send it.
    """
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"


class TavilyClient(Protocol):
    """Protocol so the concrete impl can be swapped if Tavily becomes unsuitable."""

    def search_news(
        self, query: str, *, days: int = 7, max_results: int = 5
    ) -> list[NewsResult]: ...

    def search_finance(
        self, query: str, *, days: int = 7, max_results: int = 5
    ) -> list[NewsResult]: ...


class TavilyConcreteClient:
    """Wraps the tavily-python SDK.  All exceptions are caught and return []."""

    def __init__(self, api_key: str, monthly_searches_cap: int = 200) -> None:
        from tavily import TavilyClient as TavilySdk  # noqa: N814

        self._client = TavilySdk(api_key=api_key)
        self._cap = monthly_searches_cap
        self._used_this_month: int = 0

    def search_news(
        self, query: str, *, days: int = 7, max_results: int = 5
    ) -> list[NewsResult]:
        return self._search(query, topic="news", days=days, max_results=max_results)

    def search_finance(
        self, query: str, *, days: int = 7, max_results: int = 5
    ) -> list[NewsResult]:
        return self._search(query, topic="finance", days=days, max_results=max_results)

    def _search(
        self,
        query: str,
        *,
        topic: Literal["news", "finance"],
        days: int,
        max_results: int,
    ) -> list[NewsResult]:
        if self._used_this_month >= self._cap:
            log.warning(
                "Tavily monthly cap reached (%d/%d); returning empty result for: %s",
                self._used_this_month,
                self._cap,
                query,
            )
            return []
        try:
            resp = self._client.search(
                query=query,
                topic=topic,
                days=days,
                # `days` is honored only for topic="news"; `time_range` filters recency
                # for every topic — without it, finance searches return stale matches.
                time_range=_days_to_time_range(days),
                max_results=max_results,
                search_depth="advanced",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Tavily search failed for %r: %s", query, exc, exc_info=True)
            return []

        self._used_this_month += 1
        return [
            NewsResult(
                title=str(r.get("title") or ""),
                url=str(r.get("url") or ""),
                content=(str(r.get("content") or ""))[:1500],
                published_date=_parse_date(r.get("published_date")),
                source_domain=_domain(str(r.get("url") or "")),
                score=float(r.get("score") or 0.0),
            )
            for r in resp.get("results", [])
            if r.get("url")
        ]


class FakeTavilyClient:
    """Test double — records calls, returns canned results keyed by query string."""

    def __init__(self, canned: dict[str, list[NewsResult]] | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []  # (query, topic, days)
        self._canned: dict[str, list[NewsResult]] = canned or {}

    def search_news(
        self, query: str, *, days: int = 7, max_results: int = 5
    ) -> list[NewsResult]:
        self.calls.append((query, "news", days))
        return self._canned.get(query, [])

    def search_finance(
        self, query: str, *, days: int = 7, max_results: int = 5
    ) -> list[NewsResult]:
        self.calls.append((query, "finance", days))
        return self._canned.get(query, [])


def make_tavily_client(settings: Settings) -> TavilyClient:
    """Factory — returns FakeTavilyClient when no API key is configured."""
    if not settings.tavily_api_key:
        log.warning(
            "TAVILY_API_KEY not set; Phase 4.5 weekly market context will be skipped"
        )
        return FakeTavilyClient()
    return TavilyConcreteClient(
        api_key=settings.tavily_api_key,
        monthly_searches_cap=settings.tavily_monthly_cap,
    )
