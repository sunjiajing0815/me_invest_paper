"""LLM-scored S/R levels: wraps LLMClient to produce confidence-weighted ScoredLevel list."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import pandas as pd
from pydantic import BaseModel
from sqlalchemy.orm import Session

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

    user = json.dumps(
        {
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
        },
        default=str,
    )

    system = load_prompt("score_levels_v1.txt")

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
    return out
