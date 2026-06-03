"""Weekly market context — Tavily fanout queries + Sonnet synthesis.

Produces a WeeklyMarketContext frozen dataclass for the Friday review email.
Results are informational only and must never influence the suggestion engine
or order-execution path (see ADR-0020).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import WeeklyMarketContextRow
from .llm import SONNET, LLMClient, load_prompt, persist_llm_call_log
from .sentiment import SentimentClient
from .tavily import NewsResult, TavilyClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ticker → sector mapping (extend as watchlist grows)
# ---------------------------------------------------------------------------

_TICKER_SECTORS: dict[str, str] = {
    # Broad market ETFs
    "VOO": "broad market", "SPY": "broad market", "IVV": "broad market",
    "IWM": "small cap",
    # Factor / dividend ETFs
    "QQQ": "technology", "SCHD": "dividend equity", "VIG": "dividend equity",
    "VYM": "dividend equity",
    # Technology
    "AAPL": "technology", "MSFT": "technology", "NVDA": "technology",
    "GOOG": "technology", "GOOGL": "technology", "META": "technology",
    "ORCL": "technology", "CRM": "technology", "ADBE": "technology",
    "NFLX": "technology", "AMD": "semiconductors", "INTC": "semiconductors",
    "MU": "semiconductors", "QCOM": "semiconductors", "AVGO": "semiconductors",
    "TSM": "semiconductors", "AMAT": "semiconductors",
    # Consumer
    "AMZN": "consumer discretionary", "TSLA": "consumer discretionary",
    "NKE": "consumer discretionary", "HD": "consumer discretionary",
    "COST": "consumer staples", "WMT": "consumer staples", "PG": "consumer staples",
    # Financials
    "JPM": "financials", "BAC": "financials", "GS": "financials",
    "MS": "financials", "V": "financials", "MA": "financials",
    # Healthcare
    "JNJ": "healthcare", "UNH": "healthcare", "PFE": "healthcare",
    "ABBV": "healthcare", "LLY": "healthcare",
    # Energy
    "XOM": "energy", "CVX": "energy",
    # Industrials / other
    "CAT": "industrials", "BA": "industrials", "GE": "industrials",
    "BRK.B": "financials",
}


def _infer_sectors(watchlist: list[str]) -> list[str]:
    """Return sorted unique sector names for the given watchlist tickers."""
    sectors = {_TICKER_SECTORS.get(t, "equity") for t in watchlist}
    return sorted(sectors)


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeeklyMarketContext:
    """All data for the Weekly Market Context section. Plain types only."""

    week_of: date
    macro_summary: str
    sector_summary: str
    ticker_catchup: dict[str, str]  # ticker → 1-2 sentence catch-up
    forward_events: list[str]       # bullet points: earnings, Fed, etc.
    citations: list[NewsResult]     # deduped by URL, sorted score desc, capped 15
    vix: float | None = None            # CBOE VIX close at time of synthesis
    fear_greed_score: int | None = None  # CNN Fear & Greed 0–100
    fear_greed_label: str | None = None  # "Extreme Fear" … "Extreme Greed"


class _WeeklyContextSynthesis(BaseModel):
    """LLM output schema — validated by llm.call's response_schema parameter."""

    macro_summary: str
    sector_summary: str
    ticker_catchup: dict[str, str]
    forward_events: list[str]


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def persist_weekly_context(s: Session, ctx: WeeklyMarketContext) -> None:
    """Serialize ctx and add a WeeklyMarketContextRow. Caller commits."""
    s.add(WeeklyMarketContextRow(
        week_of=ctx.week_of,
        payload_json=json.dumps(dataclasses.asdict(ctx), default=str),
    ))


def load_latest_weekly_context(
    s: Session, *, week_of: date, max_age_days: int
) -> WeeklyMarketContext | None:
    """Load the most recent context for week_of; return None if absent or stale."""
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    row = s.scalars(
        select(WeeklyMarketContextRow)
        .where(WeeklyMarketContextRow.week_of == week_of)
        .where(WeeklyMarketContextRow.created_at >= cutoff)
        .order_by(WeeklyMarketContextRow.created_at.desc())
    ).first()
    if row is None:
        log.info("load_latest_weekly_context: no fresh context for %s", week_of)
        return None
    return _weekly_context_from_dict(json.loads(row.payload_json))


def _weekly_context_from_dict(data: dict[str, Any]) -> WeeklyMarketContext:
    """Reconstruct WeeklyMarketContext from asdict() output."""
    citations = [
        NewsResult(
            title=c.get("title", ""),
            url=c.get("url", ""),
            content=c.get("content", ""),
            published_date=(
                date.fromisoformat(c["published_date"])
                if c.get("published_date")
                else None
            ),
            source_domain=c.get("source_domain", ""),
            score=float(c.get("score", 0.0)),
        )
        for c in data.get("citations", [])
    ]
    week_of_raw = data["week_of"]
    week_of = date.fromisoformat(week_of_raw) if isinstance(week_of_raw, str) else week_of_raw
    vix_raw = data.get("vix")
    fg_score_raw = data.get("fear_greed_score")
    return WeeklyMarketContext(
        week_of=week_of,
        macro_summary=data.get("macro_summary", ""),
        sector_summary=data.get("sector_summary", ""),
        ticker_catchup=data.get("ticker_catchup", {}),
        forward_events=data.get("forward_events", []),
        citations=citations,
        vix=float(vix_raw) if vix_raw is not None else None,
        fear_greed_score=int(fg_score_raw) if fg_score_raw is not None else None,
        fear_greed_label=data.get("fear_greed_label"),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_weekly_market_context(
    *,
    tavily: TavilyClient,
    llm: LLMClient,
    session: Session,
    watchlist: list[str],
    week_of: date,
    prompt_version: str = "1",
    sentiment_client: SentimentClient | None = None,
) -> WeeklyMarketContext | None:
    """Fanout Tavily queries, synthesise with Sonnet, return WeeklyMarketContext.

    Returns None when Tavily returns nothing (capped or unavailable) so the
    caller can omit the section gracefully rather than render a broken one.

    ``session`` is used only to persist the synthesis LLMCallLog row (cost/latency/temp/
    cache tiers) so weekly-context spend shows up in the audit log like every other call.
    """
    from .sentiment import FakeSentimentClient
    _sentiment = sentiment_client if sentiment_client is not None else FakeSentimentClient()
    # 1. Macro queries
    macro: list[NewsResult] = []
    macro += tavily.search_news("US Federal Reserve policy this week", days=7, max_results=4)
    macro += tavily.search_finance("US equity market this week", days=7, max_results=3)

    # 2. Sector queries
    sector_results: list[NewsResult] = []
    for sector in _infer_sectors(watchlist):
        sector_results += tavily.search_finance(
            f"{sector} sector news this week", days=7, max_results=3
        )

    # 3. Per-ticker catch-up
    ticker_results: dict[str, list[NewsResult]] = {}
    for ticker in watchlist:
        ticker_results[ticker] = tavily.search_finance(
            f"{ticker} stock news this week", days=7, max_results=3
        )

    # 4. Forward-looking events
    forward: list[NewsResult] = []
    forward += tavily.search_news(
        "US stock market earnings calendar next week", days=2, max_results=5
    )
    forward += tavily.search_news(
        "US Federal Reserve next week schedule", days=2, max_results=3
    )

    # If everything is empty, skip the section entirely
    all_ticker_results = [r for rs in ticker_results.values() for r in rs]
    if not (macro or sector_results or all_ticker_results or forward):
        log.info("build_weekly_market_context: all Tavily results empty; skipping section")
        return None

    # 5. Collect and deduplicate citations now (needed even on LLM failure)
    all_results = macro + sector_results + all_ticker_results + forward
    seen_urls: set[str] = set()
    unique_citations: list[NewsResult] = []
    for r in sorted(all_results, key=lambda x: -x.score):
        if r.url not in seen_urls:
            seen_urls.add(r.url)
            unique_citations.append(r)
    citations = unique_citations[:15]

    # 6. Market sentiment (best-effort; None if key absent or endpoint unavailable)
    sd = _sentiment.get_sentiment()
    vix = sd.vix
    fear_greed_score = sd.fear_greed_score
    fear_greed_label = sd.fear_greed_label

    # 7. Sonnet synthesis
    user_payload = json.dumps(
        {
            "week_of": str(week_of),
            "watchlist": watchlist,
            "macro_results": [
                {
                    "title": r.title,
                    "content": r.content,
                    "source_domain": r.source_domain,
                    "published_date": str(r.published_date) if r.published_date else None,
                }
                for r in macro[:6]
            ],
            "sector_results": [
                {
                    "title": r.title,
                    "content": r.content,
                    "source_domain": r.source_domain,
                }
                for r in sector_results[:8]
            ],
            "ticker_results": {
                ticker: [
                    {"title": r.title, "content": r.content, "source_domain": r.source_domain}
                    for r in rs[:3]
                ]
                for ticker, rs in ticker_results.items()
                if rs
            },
            "forward_results": [
                {"title": r.title, "content": r.content, "source_domain": r.source_domain}
                for r in forward[:6]
            ],
        },
        default=str,
    )

    system_prompt = load_prompt(f"weekly_context_v{prompt_version}.txt")
    resp, parsed = llm.call(
        model=SONNET,
        system=system_prompt,
        user=user_payload,
        max_tokens=2048,
        response_schema=_WeeklyContextSynthesis,
        temperature=0.3,  # synthesised prose, not structured labels
    )
    persist_llm_call_log(
        session,
        resp,
        purpose="weekly_context",
        status="ok" if parsed is not None else "schema_error",
    )

    if parsed is None:
        log.warning(
            "build_weekly_market_context: LLM synthesis failed; "
            "returning empty sections with citations only"
        )
        return WeeklyMarketContext(
            week_of=week_of,
            macro_summary="",
            sector_summary="",
            ticker_catchup={},
            forward_events=[],
            citations=citations,
            vix=vix,
            fear_greed_score=fear_greed_score,
            fear_greed_label=fear_greed_label,
        )

    synthesis: _WeeklyContextSynthesis = parsed  # type: ignore[assignment]
    return WeeklyMarketContext(
        week_of=week_of,
        macro_summary=synthesis.macro_summary,
        sector_summary=synthesis.sector_summary,
        ticker_catchup=synthesis.ticker_catchup,
        forward_events=synthesis.forward_events,
        citations=citations,
        vix=vix,
        fear_greed_score=fear_greed_score,
        fear_greed_label=fear_greed_label,
    )
