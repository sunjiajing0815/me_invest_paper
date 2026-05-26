"""Earnings calendar client — fetches upcoming earnings for the suggestion engine."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)


class EarningsClient(Protocol):
    """Protocol for fetching upcoming earnings dates."""

    def upcoming_earnings(self, tickers: list[str], *, start: date, end: date) -> dict[str, date]:
        """Return {ticker: earliest_earnings_date} for tickers with earnings in [start, end]."""
        ...


class FinnhubEarningsClient:
    """Wraps finnhub.Client to fetch earnings calendar data."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def upcoming_earnings(self, tickers: list[str], *, start: date, end: date) -> dict[str, date]:
        """Fetch earnings from Finnhub API for the given date range."""
        import finnhub  # lazy import — only import at call time

        client = finnhub.Client(api_key=self._api_key)
        result: dict[str, date] = {}
        for ticker in tickers:
            try:
                data = client.earnings_calendar(
                    _from=start.isoformat(), to=end.isoformat(), symbol=ticker
                )
                events = data.get("earningsCalendar") or []
                dates = [
                    date.fromisoformat(e["date"])
                    for e in events
                    if e.get("date")
                ]
                if dates:
                    result[ticker] = min(dates)
            except Exception as exc:
                log.warning("FinnhubEarningsClient: failed for %s: %s", ticker, exc)
        return result


@dataclass
class FakeEarningsClient:
    """Test double. Pre-seed with {ticker: date} canned earnings."""

    _canned: dict[str, date]
    calls: list[tuple[list[str], date, date]] = field(default_factory=list)

    def upcoming_earnings(self, tickers: list[str], *, start: date, end: date) -> dict[str, date]:
        """Return canned earnings filtered to the requested window."""
        self.calls.append((tickers, start, end))
        return {
            t: d
            for t, d in self._canned.items()
            if t in tickers and start <= d <= end
        }


def make_earnings_client(settings: Settings) -> EarningsClient:
    """Return FinnhubEarningsClient if key is set, else FakeEarningsClient with warning."""
    if not settings.finnhub_api_key:
        log.warning("make_earnings_client: FINNHUB_API_KEY not set; earnings gate will be a no-op")
        return FakeEarningsClient(_canned={})
    return FinnhubEarningsClient(api_key=settings.finnhub_api_key)
