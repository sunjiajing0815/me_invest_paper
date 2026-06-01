"""Tests for jobs/movers.py — specifically _build_news_events dedup.

Regression: a single article can surface under multiple movers (a Micron piece tagged
to both MU and MSFT). The persistence loop only skipped url_hashes already in the DB,
so the shared article was session.add-ed twice → `UNIQUE constraint failed:
news_event.url_hash` on the next autoflush, crashing the whole movers job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from investor.jobs.movers import _build_news_events


def _raw(url_hash: str, headline: str = "headline") -> SimpleNamespace:
    return SimpleNamespace(
        url_hash=url_hash,
        published_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        source="alpaca",
        headline=headline,
        url=f"https://example.com/{url_hash}",
    )


def _final(url_hash: str, material: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        url_hash=url_hash, is_material=material, sentiment="bullish", summary="s"
    )


def test_shared_article_across_movers_inserted_once() -> None:
    """The Micron piece appears for both MU and MSFT → exactly one NewsEvent, claimed by
    the first ticker (MU). Previously this double-add crashed the job."""
    shared = "shared_hash"
    news = {
        "MU": [_raw(shared, "Micron Monday"), _raw("mu_only")],
        "MSFT": [_raw(shared, "Micron Monday"), _raw("msft_only")],
    }
    final = {"MU": [_final(shared)], "MSFT": []}

    events = _build_news_events(
        ["MU", "MSFT"], news, final, arbitrated_hashes=set(), existing_hashes=set()
    )

    hashes = [e.url_hash for e in events]
    assert hashes.count(shared) == 1
    assert set(hashes) == {shared, "mu_only", "msft_only"}
    shared_ev = next(e for e in events if e.url_hash == shared)
    assert shared_ev.ticker == "MU"  # first ticker claims the shared article
    # triage fields carried through for the claimed article
    assert shared_ev.llm_material is True
    assert shared_ev.llm_sentiment == "bullish"


def test_skips_articles_already_in_db() -> None:
    news = {"MU": [_raw("in_db"), _raw("fresh")]}
    events = _build_news_events(
        ["MU"], news, {}, arbitrated_hashes=set(), existing_hashes={"in_db"}
    )
    assert [e.url_hash for e in events] == ["fresh"]


def test_arbitrated_hash_marks_sonnet_and_flag() -> None:
    from investor.services.llm import HAIKU, SONNET

    news = {"MU": [_raw("a1"), _raw("a2")]}
    events = _build_news_events(
        ["MU"], news, {}, arbitrated_hashes={"a1"}, existing_hashes=set()
    )
    by_hash = {e.url_hash: e for e in events}
    assert by_hash["a1"].arbitrated is True and by_hash["a1"].llm_model == SONNET
    assert by_hash["a2"].arbitrated is False and by_hash["a2"].llm_model == HAIKU


def test_compute_movers_uses_prior_week_close_not_7_days_back(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """last_week_close = the previous week's last trading day (prior Friday), NOT the
    close exactly 7 days back — which on a Monday run lands two Fridays ago and inflates
    the move (the MU "+37.8%" bug)."""
    import pandas as pd

    from investor.jobs.movers import _compute_movers

    bars = tmp_path / "bars"
    bars.mkdir()

    def _bar(day: str, close: float) -> dict:
        return {
            "symbol": "FOO", "timestamp": pd.Timestamp(day),
            "open": close, "high": close, "low": close, "close": close,
            "volume": 1000, "trade_count": 10, "vwap": close,
        }

    pd.DataFrame([
        _bar("2026-05-22", 100.0),  # Fri, two weeks back   (the OLD buggy "last week")
        _bar("2026-05-28", 190.0),  # Thu, last week
        _bar("2026-05-29", 200.0),  # Fri, last week        (the CORRECT last week's close)
        _bar("2026-06-01", 220.0),  # Mon, "today"
    ]).to_parquet(bars / "FOO.parquet", index=False)

    row = _compute_movers(str(bars)).set_index("ticker").loc["FOO"]
    assert row["today_close"] == 220.0
    assert row["last_week_close"] == 200.0       # May 29 (last Fri), not May 22 ($100)
    assert round(row["pct_change"], 2) == 10.0   # 220/200-1, not the +120% the bug gave
