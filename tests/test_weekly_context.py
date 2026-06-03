"""Tests for services/weekly_context.py: fanout, synthesis, dedup, caps, call-log."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.models import Base, LLMCallLog
from investor.services.llm import LLMResponse
from investor.services.tavily import FakeTavilyClient, NewsResult
from investor.services.weekly_context import (
    _WeeklyContextSynthesis,
    build_weekly_market_context,
)


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _news(url: str = "https://example.com/a", score: float = 0.8) -> NewsResult:
    return NewsResult(
        title="Headline",
        url=url,
        content="Content",
        published_date=date(2026, 5, 12),
        source_domain="example.com",
        score=score,
    )


class _FakeLLM:
    """Returns a fixed (LLMResponse, parsed) from every call(). Records the temperature it
    was called with so the call-log assertion can verify it round-trips."""

    def __init__(self, parsed: _WeeklyContextSynthesis | None) -> None:
        self._parsed = parsed

    def call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        response_schema: object = None,
        temperature: float = 0.0,
        cache_system: bool = True,
    ) -> tuple[LLMResponse, _WeeklyContextSynthesis | None]:
        resp = LLMResponse(
            content="{}",
            model=model,
            prompt_hash="fakehash0000",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.0,
            latency_ms=1,
            temperature=temperature,
            cache_write_tokens=0,
            cache_read_tokens=0,
        )
        return resp, self._parsed


def _default_synthesis(**kwargs) -> _WeeklyContextSynthesis:
    defaults = dict(
        macro_summary="The Fed held rates steady.",
        sector_summary="Tech sector outperformed.",
        ticker_catchup={"AAPL": "Apple announced new products."},
        forward_events=["FOMC meeting Thursday"],
    )
    defaults.update(kwargs)
    return _WeeklyContextSynthesis(**defaults)


class TestBuildWeeklyMarketContext:
    _WEEK = date(2026, 5, 12)

    def test_happy_path_returns_populated_context(self, db_session: Session):
        r = _news(url="https://wsj.com/1", score=0.9)
        fake = FakeTavilyClient(canned={"US Federal Reserve policy this week": [r]})
        synthesis = _default_synthesis()
        ctx = build_weekly_market_context(
            tavily=fake,
            llm=_FakeLLM(synthesis),
            session=db_session,
            watchlist=["AAPL"],
            week_of=self._WEEK,
        )
        assert ctx is not None
        assert ctx.macro_summary == "The Fed held rates steady."
        assert ctx.sector_summary == "Tech sector outperformed."
        assert ctx.ticker_catchup == {"AAPL": "Apple announced new products."}
        assert ctx.forward_events == ["FOMC meeting Thursday"]
        assert len(ctx.citations) == 1
        assert ctx.citations[0].url == "https://wsj.com/1"
        assert ctx.week_of == self._WEEK

    def test_all_tavily_empty_returns_none(self, db_session: Session):
        fake = FakeTavilyClient()  # all queries return []
        ctx = build_weekly_market_context(
            tavily=fake,
            llm=_FakeLLM(None),
            session=db_session,
            watchlist=["AAPL"],
            week_of=self._WEEK,
        )
        assert ctx is None

    def test_llm_parse_failure_returns_empty_sections_with_citations(self, db_session: Session):
        r = _news(url="https://example.com/1", score=0.8)
        fake = FakeTavilyClient(canned={"US Federal Reserve policy this week": [r]})
        ctx = build_weekly_market_context(
            tavily=fake,
            llm=_FakeLLM(None),  # parsed=None simulates LLM schema failure
            session=db_session,
            watchlist=["AAPL"],
            week_of=self._WEEK,
        )
        assert ctx is not None
        assert ctx.macro_summary == ""
        assert ctx.sector_summary == ""
        assert ctx.ticker_catchup == {}
        assert ctx.forward_events == []
        assert len(ctx.citations) > 0  # citations still populated from Tavily results

    def test_dedup_citations_by_url(self, db_session: Session):
        shared_url = "https://example.com/shared"
        r_high = _news(url=shared_url, score=0.9)
        r_low = _news(url=shared_url, score=0.3)  # same URL, lower score
        r_other = _news(url="https://example.com/other", score=0.7)
        canned = {
            "US Federal Reserve policy this week": [r_high],
            "US equity market this week": [r_low, r_other],
        }
        fake = FakeTavilyClient(canned=canned)
        ctx = build_weekly_market_context(
            tavily=fake,
            llm=_FakeLLM(_default_synthesis()),
            session=db_session,
            watchlist=["AAPL"],
            week_of=self._WEEK,
        )
        assert ctx is not None
        urls = [c.url for c in ctx.citations]
        assert len(urls) == len(set(urls)), "duplicate URLs found in citations"
        assert urls.count(shared_url) == 1

    def test_citations_capped_at_15(self, db_session: Session):
        # 8 macro + 8 forward = 16 unique URLs → should be capped to 15
        macro = [_news(url=f"https://macro.com/{i}", score=i / 100) for i in range(1, 9)]
        forward = [_news(url=f"https://fwd.com/{i}", score=i / 100) for i in range(1, 9)]
        canned = {
            "US Federal Reserve policy this week": macro[:4],
            "US equity market this week": macro[4:],
            "US stock market earnings calendar next week": forward[:5],
            "US Federal Reserve next week schedule": forward[5:],
        }
        fake = FakeTavilyClient(canned=canned)
        ctx = build_weekly_market_context(
            tavily=fake,
            llm=_FakeLLM(_default_synthesis()),
            session=db_session,
            watchlist=["AAPL"],  # minimises sector/ticker queries (→ [])
            week_of=self._WEEK,
        )
        assert ctx is not None
        assert len(ctx.citations) == 15

    def test_synthesis_persists_llm_call_log(self, db_session: Session):
        """Regression: the Sonnet synthesis must land in llm_call_log (was bypassed)."""
        r = _news(url="https://wsj.com/1", score=0.9)
        fake = FakeTavilyClient(canned={"US Federal Reserve policy this week": [r]})
        build_weekly_market_context(
            tavily=fake,
            llm=_FakeLLM(_default_synthesis()),
            session=db_session,
            watchlist=["AAPL"],
            week_of=self._WEEK,
        )
        rows = db_session.scalars(
            select(LLMCallLog).where(LLMCallLog.purpose == "weekly_context")
        ).all()
        assert len(rows) == 1
        assert rows[0].status == "ok"
        assert rows[0].temperature == 0.3  # prose temperature round-trips into the log
