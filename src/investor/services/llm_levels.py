"""LLM-scored S/R levels: wraps LLMClient to produce confidence-weighted ScoredLevel list."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..models import SRLevel as SRLevelORM
from .analytics import duckdb_conn
from .levels import SRLevelRow
from .llm import SONNET, LLMClient, LLMResponse, load_prompt, persist_llm_call_log

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoredLevel:
    """An SRLevelRow decorated with LLM-assigned confidence and rationale."""

    method: str
    price: float        # taken from the original SRLevelRow — LLM cannot change this
    type: str           # "support" | "resistance"  (carried from SRLevelRow)
    confidence: float   # clamped [0.0, 1.0]
    rationale: str      # truncated to 240 chars


# ---------------------------------------------------------------------------
# Pydantic schemas for structured output validation
# ---------------------------------------------------------------------------

class _ScoredLevelOut(BaseModel):
    method: str
    confidence: float
    rationale: str


class _LevelScoreSchema(BaseModel):
    levels: list[_ScoredLevelOut]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_levels_for_ticker(
    *,
    llm: LLMClient,
    session: Session,
    ticker: str,
    computed_levels: list[SRLevelRow],
    bars_dir: str,
    recent_news: list[dict[str, str]] | None = None,
    prompt_version: str = "2",
) -> list[ScoredLevel]:
    """Score S/R levels for one ticker using Claude Sonnet.

    On LLM failure or schema validation error: logs a warning, persists the error row,
    and returns [] so the caller falls back to nearest-distance anchor selection.
    """
    if not computed_levels:
        return []

    # --- Build OHLCV context (last 60 bars) ---
    recent_bars: list[dict[str, object]] = []
    try:
        with duckdb_conn(bars_dir) as con:
            df: pd.DataFrame = con.execute(
                "SELECT date, open, high, low, close, volume"
                " FROM price_bar WHERE ticker = ? ORDER BY date DESC LIMIT 60",
                [ticker],
            ).df()
        if not df.empty:
            df = df.sort_values("date")
            recent_bars = df.to_dict(orient="records")
    except Exception as exc:
        logger.warning("score_levels_for_ticker: bar fetch failed for %s: %s", ticker, exc)

    current_price = float(recent_bars[-1]["close"]) if recent_bars else 0.0  # type: ignore[arg-type]

    # Only score levels within 30% of current price — distant levels are never chosen
    # by select_anchor() (8% band) and generating rationales for them wastes tokens.
    nearby_levels = (
        [lv for lv in computed_levels
         if current_price > 0 and abs(lv.price / current_price - 1) <= 0.30]
        or computed_levels  # fallback: send all if current_price unknown
    )

    payload: dict[str, object] = {
        "ticker": ticker,
        "current_price": current_price,
        "recent_bars_60d": recent_bars,
        "computed_levels": [
            {
                "method": lv.method,
                "price": lv.price,
                "type": lv.type,
                "as_of": str(lv.as_of),
            }
            for lv in nearby_levels
        ],
    }
    if recent_news:
        payload["recent_material_news"] = recent_news

    user = json.dumps(payload, default=str)
    system = load_prompt(f"score_levels_v{prompt_version}.txt")

    resp: LLMResponse
    try:
        resp, parsed = llm.call(
            model=SONNET,
            system=system,
            user=user,
            max_tokens=8192,
            response_schema=_LevelScoreSchema,
        )
    except Exception as exc:
        logger.warning("score_levels_for_ticker: LLM call failed for %s: %s", ticker, exc)
        return []

    error_msg: str | None = None
    if parsed is None:
        from .llm import _strip_fences
        try:
            _LevelScoreSchema.model_validate_json(_strip_fences(resp.content))
        except Exception as exc:
            error_msg = str(exc)[:500]
    status = "ok" if parsed is not None else "schema_error"
    persist_llm_call_log(session, resp, purpose="score_levels", status=status, error=error_msg)

    if parsed is None:
        logger.warning("level scoring failed for %s; engine will fall back to nearest", ticker)
        return []

    # --- Build ScoredLevel list, drop any methods the LLM invented ---
    method_map = {lv.method: lv for lv in computed_levels}
    out: list[ScoredLevel] = []
    for entry in parsed.levels:
        original = method_map.get(entry.method)
        if original is None:
            logger.debug(
                "score_levels_for_ticker: LLM returned unknown method %r for %s — dropped",
                entry.method, ticker,
            )
            continue
        out.append(
            ScoredLevel(
                method=entry.method,
                price=original.price,
                type=original.type,
                confidence=max(0.0, min(1.0, entry.confidence)),
                rationale=entry.rationale[:240],
            )
        )

    # --- Write scores back to sr_level rows so Phase 3c can query them from the DB ---
    scored_at = datetime.now(UTC)
    for scored in out:
        original = method_map[scored.method]
        orm_row = (
            session.query(SRLevelORM)
            .filter_by(ticker=ticker, method=scored.method, as_of=original.as_of)
            .first()
        )
        if orm_row is not None:
            orm_row.confidence = scored.confidence
            orm_row.llm_rationale = scored.rationale
            orm_row.scored_at = scored_at
            orm_row.scored_by_model = SONNET
            orm_row.prompt_version = prompt_version
    session.flush()

    return out


def load_latest_scored_levels(
    session: Session, *, max_age_days: int = 7
) -> dict[str, list[ScoredLevel]]:
    """Load the most-recently-scored S/R levels per ticker from the DB.

    Returns only rows where confidence IS NOT NULL (i.e., LLM-scored rows) whose
    ``as_of`` is within ``max_age_days``. The staleness cutoff matters: without it, a
    ticker whose scoring stopped persisting kept serving months-old prices to the
    review graph — context_adjust re-anchored MU onto a June sma_20 at \\$751 when the
    stock traded \\$979 (post-4.9a §16). A ticker with no FRESH scored rows is simply
    absent, which the graph already treats as "no scored levels" (re-anchor skipped).

    Per ticker, returns all scored rows from the latest fresh ``as_of`` date, sorted by
    confidence descending.
    """
    cutoff = datetime.now(UTC).date() - timedelta(days=max_age_days)
    rows = (
        session.query(SRLevelORM)
        .filter(SRLevelORM.confidence.isnot(None), SRLevelORM.as_of >= cutoff)
        .all()
    )

    # Group by ticker, tracking max as_of per ticker
    from collections import defaultdict

    ticker_rows: dict[str, list[SRLevelORM]] = defaultdict(list)
    for row in rows:
        ticker_rows[row.ticker].append(row)

    result: dict[str, list[ScoredLevel]] = {}
    for ticker, ticker_level_rows in ticker_rows.items():
        max_as_of = max(r.as_of for r in ticker_level_rows)
        latest_rows = [r for r in ticker_level_rows if r.as_of == max_as_of]
        scored = [
            ScoredLevel(
                method=r.method,
                price=r.price,
                type=r.type,
                confidence=float(r.confidence),  # type: ignore[arg-type]
                rationale=r.llm_rationale or "",
            )
            for r in latest_rows
        ]
        scored.sort(key=lambda s: s.confidence, reverse=True)
        result[ticker] = scored

    return result
