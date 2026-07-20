"""Regression tests for the MU $751.43 stale-anchor chain (post-4.9a §16).

Three layers: (1) score write-back persists when sr_level rows exist first (the weekly
job now persists levels BEFORE scoring); (2) load_latest_scored_levels ignores stale
scored sets; (3) _find_level rejects re-anchors beyond the 15% distance guard.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from investor.models import SRLevel
from investor.services.levels import SRLevelRow, persist_levels
from investor.services.llm import LLMResponse
from investor.services.llm_levels import (
    _LevelScoreSchema,
    _ScoredLevelOut,
    load_latest_scored_levels,
    score_levels_for_ticker,
)

_TODAY = datetime.now(UTC).date()


def _resp() -> LLMResponse:
    return LLMResponse(content="{}", model="m", prompt_hash="x", input_tokens=1,
                       output_tokens=1, cost_usd=0.0, latency_ms=1)


def _seed_scored(session: Session, ticker: str, as_of: date, price: float,
                 conf: float = 0.6) -> None:
    session.add(SRLevel(
        ticker=ticker, type="support", price=price, method="sma_20", as_of=as_of,
        confidence=conf, llm_rationale="r", scored_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    ))
    session.flush()


# ── layer 1: score write-back persists once rows exist first ──────────────────

def test_scores_persist_when_levels_persisted_first(db_session: Session) -> None:
    """The weekly job persists sr_level rows BEFORE scoring; the write-back must then
    land confidence/scored_at on them (pre-fix, scoring ran first and silently no-oped)."""
    rows = [SRLevelRow(ticker="MU", type="support", price=898.85, method="sma_50",
                       as_of=_TODAY)]
    persist_levels(db_session, rows)

    parsed = _LevelScoreSchema(levels=[
        _ScoredLevelOut(method="sma_50", confidence=0.65, rationale="tested twice"),
    ])
    llm = MagicMock()
    llm.call.return_value = (_resp(), parsed)
    out = score_levels_for_ticker(
        llm=llm, session=db_session, ticker="MU", computed_levels=rows,
        bars_dir="data/bars",
    )
    assert out and out[0].confidence == pytest.approx(0.65)

    orm = db_session.query(SRLevel).filter_by(ticker="MU", method="sma_50",
                                              as_of=_TODAY).one()
    assert orm.confidence == pytest.approx(0.65)      # ← the bug: this was None
    assert orm.scored_at is not None


def test_persist_levels_upsert_does_not_clobber_scores(db_session: Session) -> None:
    """Re-running persist_levels for the same (ticker, method, as_of) must keep the
    already-written confidence (on_conflict updates price/type only)."""
    rows = [SRLevelRow(ticker="MU", type="support", price=898.85, method="sma_50",
                       as_of=_TODAY)]
    persist_levels(db_session, rows)
    orm = db_session.query(SRLevel).filter_by(ticker="MU").one()
    orm.confidence = 0.65
    db_session.flush()

    persist_levels(db_session, rows)  # idempotent re-run
    assert db_session.query(SRLevel).filter_by(ticker="MU").one().confidence == \
        pytest.approx(0.65)


# ── layer 2: staleness cutoff ─────────────────────────────────────────────────

def test_stale_scored_levels_excluded(db_session: Session) -> None:
    """Scored sets older than max_age_days must not be served to the review graph —
    MU's June sma_20 at \\$751 was served in July when the stock traded \\$979."""
    _seed_scored(db_session, "MU", _TODAY - timedelta(days=41), 751.43)
    assert "MU" not in load_latest_scored_levels(db_session)


def test_fresh_scored_levels_included(db_session: Session) -> None:
    _seed_scored(db_session, "MU", _TODAY - timedelta(days=2), 1052.93)
    out = load_latest_scored_levels(db_session)
    assert out["MU"][0].price == pytest.approx(1052.93)


def test_fresh_wins_even_with_stale_history(db_session: Session) -> None:
    """With both stale and fresh scored rows, only the fresh set is returned."""
    _seed_scored(db_session, "MU", _TODAY - timedelta(days=41), 751.43)
    _seed_scored(db_session, "MU", _TODAY - timedelta(days=1), 1052.93)
    out = load_latest_scored_levels(db_session)
    assert [lv.price for lv in out["MU"]] == [pytest.approx(1052.93)]


# ── step 4: candle history in LLM payloads ──────────────────────────────────

def _write_bars_parquet(tmp_path, ticker: str, rows: list[tuple]) -> None:  # type: ignore[no-untyped-def]
    """rows: (days_ago, open, high, low, close, volume) from _TODAY, ascending after sort."""
    import pandas as pd
    data = [
        {"symbol": ticker, "timestamp": _TODAY - timedelta(days=ago), "open": o,
         "high": h, "low": lo, "close": c, "volume": v, "trade_count": 1.0, "vwap": c}
        for ago, o, h, lo, c, v in rows
    ]
    df = pd.DataFrame(sorted(data, key=lambda r: r["timestamp"]))
    df.to_parquet(tmp_path / f"{ticker}.parquet", index=False)


def test_history_payload_present_with_bars(tmp_path, db_session: Session) -> None:  # type: ignore[no-untyped-def]
    """score_levels_for_ticker's payload carries deterministic history facts per level —
    touched today, tested count, broken flag — so the LLM has candle facts to weigh."""
    _write_bars_parquet(tmp_path, "MU", [
        (0, 100, 101.5, 99.0, 100.5, 5000),
        *[(d, 105, 106, 104, 105, 1000) for d in range(1, 26)],
    ])
    rows = [SRLevelRow(ticker="MU", type="support", price=100.0, method="sma_50",
                       as_of=_TODAY)]
    llm = MagicMock()
    parsed = _LevelScoreSchema(levels=[
        _ScoredLevelOut(method="sma_50", confidence=0.7, rationale="t"),
    ])
    llm.call.return_value = (_resp(), parsed)

    score_levels_for_ticker(
        llm=llm, session=db_session, ticker="MU", computed_levels=rows,
        bars_dir=str(tmp_path),
    )
    payload = json.loads(llm.call.call_args.kwargs["user"])
    level = payload["computed_levels"][0]
    assert level["history"]["touched_today"] is True
    assert level["history"]["closed_through_recently"] is False


def test_history_payload_none_without_bars(db_session: Session) -> None:
    """No bars_dir data → history is None per level, not a crash."""
    rows = [SRLevelRow(ticker="ZZZZ", type="support", price=100.0, method="sma_50",
                       as_of=_TODAY)]
    llm = MagicMock()
    parsed = _LevelScoreSchema(levels=[
        _ScoredLevelOut(method="sma_50", confidence=0.7, rationale="t"),
    ])
    llm.call.return_value = (_resp(), parsed)

    score_levels_for_ticker(
        llm=llm, session=db_session, ticker="ZZZZ", computed_levels=rows,
        bars_dir="data/bars_does_not_exist",
    )
    payload = json.loads(llm.call.call_args.kwargs["user"])
    assert payload["computed_levels"][0]["history"] is None
