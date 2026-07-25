"""Weekly-review reflection: turn resolved suggestion outcomes + news into generalizable
methodology lessons (plans/pre_phase5_features_design.md §4).

Two layers:
  - build_outcomes(): pure, deterministic evidence rows (suggested vs fill vs current vs news).
  - reflect_on_week(): a single Sonnet synthesis over that evidence + prior insights, returning
    generalized lessons. Methodology observations ONLY — the prompt forbids price targets /
    trade recommendations, and the output never touches the suggestion engine.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ReflectionInsight
from .llm import SONNET, LLMClient, load_prompt, persist_llm_call_log

if TYPE_CHECKING:
    from ..jobs.weekly_review import SuggestionAudit

logger = logging.getLogger(__name__)

# order_suggestion.status values that represent a resolved (acted / terminal) outcome.
_RESOLVED = {"filled", "expired", "accepted", "rejected"}


@dataclass(frozen=True)
class SuggestionOutcome:
    """Deterministic evidence row for one resolved suggestion — the reflection's input and
    the email's evidence table. Not persisted."""

    ticker: str
    side: str
    limit_price: float
    filled_price: float | None
    current_close: float | None
    entry_vs_current_pct: float | None  # (fill-or-limit / current − 1) × 100
    outcome: str                        # filled | expired_unfilled | accepted_unfilled | rejected
    news_sentiment: str | None


@dataclass(frozen=True)
class ReflectionInsightRow:
    """Plain-data generalized lesson — safe after session closes."""

    category: str
    lesson: str
    tickers: list[str]
    relation_to_prior: str | None  # confirms | contradicts | None


def _outcome_label(status: str, filled_price: float | None) -> str | None:
    """Map an order_suggestion status to a resolved-outcome label (None if not resolved)."""
    if status == "filled" or (status == "accepted" and filled_price is not None):
        return "filled"
    if status == "expired":
        return "expired_unfilled"
    if status == "accepted":  # accepted but no fill
        return "accepted_unfilled"
    if status == "rejected":
        return "rejected"
    return None  # pending / anything else — not resolved


def build_outcomes(
    audits: list[SuggestionAudit],
    *,
    current_close: dict[str, float],
    news_sentiment: dict[str, str | None],
) -> list[SuggestionOutcome]:
    """Build deterministic evidence rows for the RESOLVED suggestions of the week.

    ``entry_vs_current_pct`` uses the fill price when filled, else the limit price (the level
    that never came) — both measured against the current close. Pending suggestions are
    skipped. Pure — no I/O."""
    out: list[SuggestionOutcome] = []
    for a in audits:
        label = _outcome_label(a.status, a.filled_price)
        if label is None:
            continue
        cur = current_close.get(a.ticker)
        entry = a.filled_price if a.filled_price is not None else a.limit_price
        gap = (entry / cur - 1) * 100 if cur else None
        out.append(SuggestionOutcome(
            ticker=a.ticker,
            side=a.side,
            limit_price=a.limit_price,
            filled_price=a.filled_price,
            current_close=cur,
            entry_vs_current_pct=gap,
            outcome=label,
            news_sentiment=news_sentiment.get(a.ticker),
        ))
    return out


# ── LLM synthesis ─────────────────────────────────────────────────────────────

class _ReflectionInsightOut(BaseModel):
    category: str
    lesson: str
    tickers: list[str]
    relation_to_prior: str | None = None


class _ReflectionOutput(BaseModel):
    insights: list[_ReflectionInsightOut]


def load_recent_insights(
    session: Session, broker_account_id: int, *, limit: int
) -> list[ReflectionInsightRow]:
    """The last ``limit`` stored insights for this account (newest first) — the learning loop."""
    rows = session.scalars(
        select(ReflectionInsight)
        .where(ReflectionInsight.broker_account_id == broker_account_id)
        .order_by(ReflectionInsight.created_at.desc())
        .limit(limit)
    ).all()
    return [
        ReflectionInsightRow(
            category=r.category,
            lesson=r.lesson,
            tickers=json.loads(r.tickers) if r.tickers else [],
            relation_to_prior=r.relation_to_prior,
        )
        for r in rows
    ]


def reflect_on_week(
    llm: LLMClient,
    session: Session,
    *,
    outcomes: list[SuggestionOutcome],
    prior_insights: list[ReflectionInsightRow],
    prompt_version: str = "1",
) -> list[ReflectionInsightRow]:
    """One Sonnet synthesis over the week's resolved outcomes + prior insights → generalized
    methodology lessons. Returns [] on no outcomes or any LLM/parse failure (email still
    sends). Persists an llm_call_log row (purpose="weekly_reflection")."""
    if not outcomes:
        return []

    payload = json.dumps({
        "resolved_outcomes": [
            {
                "ticker": o.ticker,
                "side": o.side,
                "limit_price": o.limit_price,
                "filled_price": o.filled_price,
                "current_close": o.current_close,
                "entry_vs_current_pct": (
                    round(o.entry_vs_current_pct, 1)
                    if o.entry_vs_current_pct is not None else None
                ),
                "outcome": o.outcome,
                "news_sentiment": o.news_sentiment,
            }
            for o in outcomes
        ],
        "prior_insights": [
            {"category": p.category, "lesson": p.lesson, "tickers": p.tickers}
            for p in prior_insights
        ],
    })

    system = load_prompt(f"weekly_reflection_v{prompt_version}.txt")
    try:
        resp, parsed = llm.call(
            model=SONNET,
            system=system,
            user=payload,
            max_tokens=1536,
            response_schema=_ReflectionOutput,
            temperature=0.3,
        )
    except Exception as exc:
        logger.warning("reflect_on_week: LLM call failed: %s", exc)
        return []

    status = "ok" if parsed is not None else "schema_error"
    persist_llm_call_log(session, resp, purpose="weekly_reflection", status=status)

    if parsed is None:
        logger.warning("reflect_on_week: schema validation failed; no insights this week")
        return []

    return [
        ReflectionInsightRow(
            category=i.category[:40],
            lesson=i.lesson[:500],
            tickers=i.tickers,
            relation_to_prior=(
                i.relation_to_prior
                if i.relation_to_prior in ("confirms", "contradicts") else None
            ),
        )
        for i in parsed.insights[:5]
    ]


def persist_insights(
    session: Session,
    insights: list[ReflectionInsightRow],
    *,
    broker_account_id: int,
    week_of: date,
) -> None:
    """Append the week's lessons to reflection_insight (the accumulating wisdom log)."""
    for ins in insights:
        session.add(ReflectionInsight(
            broker_account_id=broker_account_id,
            week_of=week_of,
            category=ins.category,
            lesson=ins.lesson,
            tickers=json.dumps(ins.tickers),
            relation_to_prior=ins.relation_to_prior,
        ))
    session.flush()
