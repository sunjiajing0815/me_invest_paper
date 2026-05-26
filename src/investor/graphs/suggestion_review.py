"""Suggestion review LangGraph workflow.

gather_context → reason → context_adjust → critic → revise/skip_revise → finalize
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel
from typing_extensions import TypedDict

from ..models import BrokerAccount
from ..services.daily_report import AccountSnapshot
from ..services.gap import GapRow, UntrackedPosition, compute_gap, get_untracked_positions
from ..services.indicators import IndicatorRow, compute_indicators
from ..services.levels import get_active_targets_id
from ..services.llm import SONNET, load_prompt
from ..services.llm_levels import ScoredLevel, load_latest_scored_levels
from ..services.news import load_recent_material_news
from ..services.suggest import OrderSuggestionRow, _ceil2dp, _floor2dp, persist_suggestions
from ..services.weekly_context import WeeklyMarketContext, load_latest_weekly_context
from . import make_checkpointer
from ._nodes import llm_node_call

# NewsTriageItem is in the news_triage graph module
from .news_triage import NewsTriageItem

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewContext:
    """Frozen snapshot of all DB/computed data needed by graph nodes."""

    gap_rows: list[GapRow]
    scored_levels: dict[str, list[ScoredLevel]]        # ticker -> confidence desc
    recent_news: dict[str, list[NewsTriageItem]]       # last 7 days, material only
    indicators: dict[str, IndicatorRow]                # keyed by ticker
    account: AccountSnapshot
    untracked_positions: list[UntrackedPosition]
    market_context: WeeklyMarketContext | None = None  # None → skip narrative pass
    # empty → gate no-ops
    earnings_by_ticker: dict[str, date] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class CriticDecision:
    """Frozen verdict from the critic LLM node."""

    verdict: Literal["approve", "revise", "reject"]
    reason: str
    suggested_changes: dict[str, Any] | None


class SuggestionReviewState(TypedDict):
    week_of: date
    context: ReviewContext
    drafts: list[OrderSuggestionRow]
    rationales: dict[int, str]                         # draft_index -> rationale text
    critic_decisions: dict[int, CriticDecision]        # draft_index -> verdict
    finals: list[OrderSuggestionRow]
    suggestion_ids: list[int]                          # IDs from persist_suggestions
    telemetry: dict  # type: ignore[type-arg]
    targets_id: int | None


# ---------------------------------------------------------------------------
# Pydantic schemas for LLM outputs
# ---------------------------------------------------------------------------


class DraftRationale(BaseModel):
    draft_index: int
    rationale: str   # 2-4 sentences, <=600 chars


class DraftRationales(BaseModel):
    items: list[DraftRationale]


class CriticDecisionOut(BaseModel):
    draft_index: int
    verdict: Literal["approve", "revise", "reject"]
    reason: str
    suggested_changes: dict[str, Any] | None = None


class CriticDecisions(BaseModel):
    items: list[CriticDecisionOut]


class DraftSizeAdjustment(BaseModel):
    draft_index: int
    size_multiplier: float
    prefer_anchor: str | None = None
    note: str


class DraftSizeAdjustments(BaseModel):
    items: list[DraftSizeAdjustment]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def gather_context_node(
    state: SuggestionReviewState,
    session_factory: Any,
    watchlist: list[str],
    bars_dir: str,
    settings: Any,
    earnings_client: Any,
) -> SuggestionReviewState:
    """Load all DB data and compute indicators; build frozen ReviewContext.

    All ORM access happens inside a single ``with session_factory()`` block.
    Indicators are computed outside (DuckDB, no session needed) after the
    session closes — this prevents DetachedInstanceError.
    """
    gap_rows: list[GapRow]
    scored_levels: dict[str, list[ScoredLevel]]
    recent_news: dict[str, list[NewsTriageItem]]
    untracked: list[UntrackedPosition]
    account: AccountSnapshot
    targets_id: int | None

    week_of: date = state["week_of"]
    earnings_by_ticker = earnings_client.upcoming_earnings(
        watchlist,
        start=week_of,
        end=week_of + timedelta(days=settings.earnings_lookahead_days),
    )

    with session_factory() as s:
        gap_rows = compute_gap(s)
        scored_levels = load_latest_scored_levels(s)
        recent_news = load_recent_material_news(s, days=7)
        untracked = get_untracked_positions(s)
        targets_id = get_active_targets_id(s)
        market_context = load_latest_weekly_context(
            s, week_of=week_of, max_age_days=settings.context_max_age_days
        )

        orm_account = (
            s.query(BrokerAccount)
            .filter(BrokerAccount.effective_to.is_(None))
            .order_by(BrokerAccount.last_sync.desc())
            .first()
        )
        account = (
            AccountSnapshot(
                broker=orm_account.broker,
                mode=orm_account.mode,
                cash_usd=float(orm_account.cash_usd),
                equity_usd=float(orm_account.equity_usd),
            )
            if orm_account is not None
            else AccountSnapshot(broker="unknown", mode="unknown", cash_usd=0.0, equity_usd=0.0)
        )

    # compute_indicators is DuckDB/Parquet — no session needed
    indicators_list = compute_indicators(watchlist, bars_dir)
    indicators: dict[str, IndicatorRow] = {row.ticker: row for row in indicators_list}

    ctx = ReviewContext(
        gap_rows=gap_rows,
        scored_levels=scored_levels,
        recent_news=recent_news,
        indicators=indicators,
        account=account,
        untracked_positions=untracked,
        market_context=market_context,
        earnings_by_ticker=earnings_by_ticker,
    )

    return {**state, "context": ctx, "targets_id": targets_id}


def reason_node(
    state: SuggestionReviewState,
    llm: Any,
    session_factory: Any,
) -> SuggestionReviewState:
    """Sonnet writes 2-4 sentence rationales for each draft order suggestion."""
    system = load_prompt("suggestion_reason_v1.txt")

    ctx = state["context"]
    drafts = state["drafts"]

    drafts_payload = [
        {
            "draft_index": i,
            "ticker": d.ticker,
            "side": d.side,
            "qty": d.qty,
            "limit_price": d.limit_price,
            "confidence_at_creation": d.confidence_at_creation,
            "anchor_method": d.anchor_method,
        }
        for i, d in enumerate(drafts)
    ]

    gap_summary = [
        {
            "ticker": g.ticker,
            "current_pct": g.current_pct,
            "target_pct": g.target_pct,
            "gap_pct": g.gap_pct,
            "gap_usd": g.gap_usd,
            "band_status": g.band_status,
        }
        for g in ctx.gap_rows
    ]

    scored_levels_payload: dict[str, list[dict[str, Any]]] = {
        ticker: [
            {
                "method": lv.method,
                "price": lv.price,
                "type": lv.type,
                "confidence": lv.confidence,
                "rationale": lv.rationale,
            }
            for lv in levels[:5]
        ]
        for ticker, levels in ctx.scored_levels.items()
    }

    news_payload: dict[str, list[dict[str, Any]]] = {
        ticker: [
            {
                "url_hash": item.url_hash,
                "is_material": item.is_material,
                "sentiment": item.sentiment,
                "summary": item.summary,
            }
            for item in items
        ]
        for ticker, items in ctx.recent_news.items()
        if any(item.is_material for item in items)
    }

    indicators_payload: dict[str, dict[str, Any]] = {
        ticker: {
            "close": row.close,
            "sma_20": row.sma_20,
            "sma_50": row.sma_50,
            "sma_200": row.sma_200,
            "ema_21": row.ema_21,
            "rsi_14": row.rsi_14,
            "macd": row.macd,
            "macd_signal": row.macd_signal,
            "pct_from_sma_50": row.pct_from_sma_50,
            "pct_from_sma_200": row.pct_from_sma_200,
        }
        for ticker, row in ctx.indicators.items()
    }

    user = json.dumps(
        {
            "drafts": drafts_payload,
            "gap_summary": gap_summary,
            "scored_levels": scored_levels_payload,
            "recent_material_news": news_payload,
            "indicators": indicators_payload,
            "account": {
                "broker": ctx.account.broker,
                "mode": ctx.account.mode,
                "cash_usd": ctx.account.cash_usd,
                "equity_usd": ctx.account.equity_usd,
            },
        },
        default=str,
    )

    with session_factory() as s:
        parsed, tel = llm_node_call(
            purpose="suggestion_reason",
            model=SONNET,
            system=system,
            user=user,
            schema=DraftRationales,
            fallback_factory=lambda: DraftRationales(items=[]),
            llm=llm,
            session=s,
        )

    rationales = {it.draft_index: it.rationale[:600] for it in parsed.items}

    return {
        **state,
        "rationales": rationales,
        "telemetry": {**state.get("telemetry", {}), **tel},
    }


def _deeper_anchor(
    draft: OrderSuggestionRow, scored_levels: dict[str, list[ScoredLevel]]
) -> ScoredLevel | None:
    """For buy: nearest support below current limit; for sell: nearest resistance above."""
    levels = scored_levels.get(draft.ticker, [])
    if draft.side == "buy":
        candidates = [lv for lv in levels if lv.type == "support" and lv.price < draft.limit_price]
        return max(candidates, key=lambda lv: lv.price) if candidates else None
    candidates = [lv for lv in levels if lv.type == "resistance" and lv.price > draft.limit_price]
    return min(candidates, key=lambda lv: lv.price) if candidates else None


def _find_level(levels: list[ScoredLevel], method: str, side: str) -> ScoredLevel | None:
    """Return level matching method only if correct type for side; None otherwise."""
    expected_type = "support" if side == "buy" else "resistance"
    return next((lv for lv in levels if lv.method == method and lv.type == expected_type), None)


def context_adjust_node(
    state: SuggestionReviewState,
    llm: Any,
    session_factory: Any,
    settings: Any,
) -> SuggestionReviewState:
    """Adjust draft quantities: earnings gate + bounded Sonnet narrative multiplier."""
    ctx = state["context"]
    drafts = list(state["drafts"])

    # --- 4a: Earnings gate (deterministic Python) ---
    earnings_factors: dict[int, float] = {}
    earnings_anchors: dict[int, ScoredLevel] = {}
    for i, d in enumerate(drafts):
        if d.ticker in ctx.earnings_by_ticker:
            earnings_factors[i] = settings.earnings_size_factor
            if settings.earnings_reanchor:
                deeper = _deeper_anchor(d, ctx.scored_levels)
                if deeper is not None:
                    earnings_anchors[i] = deeper

    # --- 4b: Narrative multiplier (Sonnet, bounded) ---
    narrative_adjustments: dict[int, DraftSizeAdjustment] = {}
    context_tel: dict[str, object] = {}
    if ctx.market_context is not None:
        system = load_prompt(f"context_size_v{settings.context_adjust_prompt_version}.txt")
        user_payload = json.dumps(
            {
                "week_of": str(state["week_of"]),
                "market_context": {
                    "macro_summary": ctx.market_context.macro_summary,
                    "sector_summary": ctx.market_context.sector_summary,
                },
                "drafts": [
                    {
                        "draft_index": i,
                        "ticker": d.ticker,
                        "side": d.side,
                        "qty": d.qty,
                        "limit_price": d.limit_price,
                        "anchor_method": d.anchor_method,
                        "scored_levels": [
                            {
                                "method": lv.method,
                                "price": lv.price,
                                "type": lv.type,
                                "confidence": lv.confidence,
                            }
                            for lv in ctx.scored_levels.get(d.ticker, [])[:5]
                        ],
                        "earnings_date": (
                            str(ctx.earnings_by_ticker[d.ticker])
                            if d.ticker in ctx.earnings_by_ticker
                            else None
                        ),
                    }
                    for i, d in enumerate(drafts)
                ],
                "sentiment": {
                    "vix": ctx.market_context.vix,
                    "fear_greed_score": ctx.market_context.fear_greed_score,
                    "fear_greed_label": ctx.market_context.fear_greed_label,
                },
                "bounds": {
                    "min": settings.context_size_min,
                    "max": settings.context_size_max,
                },
            },
            default=str,
        )
        with session_factory() as s:
            parsed, context_tel = llm_node_call(
                purpose="context_adjust",
                model=SONNET,
                system=system,
                user=user_payload,
                schema=DraftSizeAdjustments,
                fallback_factory=lambda: DraftSizeAdjustments(items=[]),
                llm=llm,
                session=s,
            )
        narrative_adjustments = {it.draft_index: it for it in parsed.items}

    # --- 4c: Apply adjustments (Python), drop sub-1-share drafts, re-key rationales ---
    rationales = dict(state.get("rationales", {}))
    adjusted: list[OrderSuggestionRow] = []
    old_to_new: dict[int, int] = {}

    for i, d in enumerate(drafts):
        ef = earnings_factors.get(i, 1.0)
        narrative = narrative_adjustments.get(i)

        nf = 1.0
        prefer_anchor: str | None = None
        note_parts: list[str] = []

        if narrative is not None:
            nf = max(
                settings.context_size_min,
                min(settings.context_size_max, narrative.size_multiplier),
            )
            prefer_anchor = narrative.prefer_anchor
            if narrative.note:
                note_parts.append(narrative.note)

        size_factor = ef * nf

        # Determine new anchor/limit (earnings anchor takes precedence over narrative prefer_anchor)
        new_limit = d.limit_price
        new_anchor = d.anchor_method
        ticker_levels = ctx.scored_levels.get(d.ticker, [])

        if i in earnings_anchors:
            lv = earnings_anchors[i]
            new_limit = _floor2dp(lv.price) if d.side == "buy" else _ceil2dp(lv.price)
            new_anchor = lv.method
            size_suffix = f", size ×{ef}" if ef != 1.0 else ""
            earnings_note = (
                f"earnings {ctx.earnings_by_ticker[d.ticker]}: "
                f"reanchored to {new_anchor}{size_suffix}"
            )
            note_parts.append(earnings_note)
        elif prefer_anchor is not None:
            found = _find_level(ticker_levels, prefer_anchor, d.side)
            if found is not None:
                new_limit = _floor2dp(found.price) if d.side == "buy" else _ceil2dp(found.price)
                new_anchor = found.method

        if ef != 1.0 and i not in earnings_anchors:
            note_parts.append(f"earnings {ctx.earnings_by_ticker.get(d.ticker)}: size ×{ef}")

        base_qty = d.qty if size_factor != 1.0 else None
        # int() truncation = floor — intentional: never exceed authorized share count
        new_qty = float(int((base_qty or d.qty) * size_factor))  # floor to whole shares
        if new_qty < 1:
            log.info(
                "context_adjust: dropping %s/%s draft (size ×%.2f → qty %.0f)",
                i, d.ticker, size_factor, new_qty,
            )
            continue

        context_note = "; ".join(note_parts) if note_parts else None
        adjusted_draft = dataclasses.replace(
            d,
            qty=new_qty,
            limit_price=new_limit,
            anchor_method=new_anchor,
            base_qty=base_qty,
            size_factor=size_factor,
            context_note=context_note,
        )
        old_to_new[i] = len(adjusted)
        adjusted.append(adjusted_draft)

    rekeyed_rationales = {
        old_to_new[old]: text for old, text in rationales.items() if old in old_to_new
    }

    return {
        **state,
        "drafts": adjusted,
        "rationales": rekeyed_rationales,
        "telemetry": {**state.get("telemetry", {}), **context_tel},
    }


def critic_node(
    state: SuggestionReviewState,
    llm: Any,
    session_factory: Any,
    settings: Any,
) -> SuggestionReviewState:
    """Sonnet reviews each draft + rationale and returns approve/revise/reject."""
    system = load_prompt(f"suggestion_critic_v{settings.critic_prompt_version}.txt")

    ctx = state["context"]
    drafts = state["drafts"]
    rationales = state.get("rationales", {})

    drafts_with_rationales = [
        {
            "draft_index": i,
            "ticker": d.ticker,
            "side": d.side,
            "qty": d.qty,
            "limit_price": d.limit_price,
            "confidence_at_creation": d.confidence_at_creation,
            "anchor_method": d.anchor_method,
            "rationale": rationales.get(i, ""),
            "base_qty": d.base_qty,
            "size_factor": d.size_factor,
            "context_note": d.context_note,
        }
        for i, d in enumerate(drafts)
    ]

    news_by_ticker: dict[str, list[dict[str, Any]]] = {
        ticker: [
            {
                "url_hash": item.url_hash,
                "sentiment": item.sentiment,
                "summary": item.summary,
            }
            for item in items
            if item.is_material
        ]
        for ticker, items in ctx.recent_news.items()
    }

    user = json.dumps(
        {
            "drafts_with_rationales": drafts_with_rationales,
            "account": {
                "cash_usd": ctx.account.cash_usd,
                "cash_floor": 100.0,
            },
            "untracked_positions": [
                {
                    "ticker": u.ticker,
                    "qty": u.qty,
                    "market_value": u.market_value,
                    "weight_pct": u.weight_pct,
                }
                for u in ctx.untracked_positions
            ],
            "recent_material_news_by_ticker": news_by_ticker,
            "earnings_by_ticker": {
                t: str(dt) for t, dt in ctx.earnings_by_ticker.items()
            },
            "market_context": (
                {
                    "macro_summary": ctx.market_context.macro_summary,
                    "sector_summary": ctx.market_context.sector_summary,
                }
                if ctx.market_context is not None
                else None
            ),
        },
        default=str,
    )

    with session_factory() as s:
        parsed, tel = llm_node_call(
            purpose="suggestion_critic",
            model=SONNET,
            system=system,
            user=user,
            schema=CriticDecisions,
            fallback_factory=lambda: CriticDecisions(items=[]),
            llm=llm,
            session=s,
        )

    decisions: dict[int, CriticDecision] = {
        it.draft_index: CriticDecision(
            verdict=it.verdict,
            reason=it.reason,
            suggested_changes=it.suggested_changes,
        )
        for it in parsed.items
    }

    return {
        **state,
        "critic_decisions": decisions,
        "telemetry": {**state.get("telemetry", {}), **tel},
    }


# ---------------------------------------------------------------------------
# Routing + revision
# ---------------------------------------------------------------------------


def route_after_critic(
    state: SuggestionReviewState,
) -> Literal["revise", "skip_revise"]:
    """Route to revise if any verdict is revise/reject; otherwise skip."""
    has_changes = any(
        d.verdict in ("revise", "reject")
        for d in state["critic_decisions"].values()
    )
    return "revise" if has_changes else "skip_revise"


def skip_revise_node(state: SuggestionReviewState) -> SuggestionReviewState:
    """All drafts approved — copy drafts straight to finals."""
    return {**state, "finals": list(state["drafts"])}


def _apply_changes(
    draft: OrderSuggestionRow,
    changes: dict[str, Any],
    ctx: ReviewContext,
) -> OrderSuggestionRow | None:
    """Apply critic-suggested field changes to a draft, validating against context.

    Returns None if the requested changes are invalid (unknown anchor method,
    price not in scored levels, or quantity < 1).
    """
    new_limit = draft.limit_price
    new_qty = draft.qty
    new_anchor: str | None = draft.anchor_method

    ticker_levels = ctx.scored_levels.get(draft.ticker, [])

    if "anchor_method" in changes:
        method = str(changes["anchor_method"])
        match = next((lv for lv in ticker_levels if lv.method == method), None)
        if match is None:
            log.debug(
                "_apply_changes: unknown anchor_method %r for %s — returning None",
                method,
                draft.ticker,
            )
            return None
        new_anchor = match.method
        new_limit = match.price

    elif "limit_price" in changes:
        requested_price = float(changes["limit_price"])
        price_match = next(
            (lv for lv in ticker_levels if abs(lv.price - requested_price) <= 0.01),
            None,
        )
        if price_match is None:
            log.debug(
                "_apply_changes: limit_price %.2f not found in scored levels for %s"
                " — returning None",
                requested_price,
                draft.ticker,
            )
            return None
        new_limit = requested_price

    if "qty" in changes:
        new_qty = max(0.0, float(changes["qty"]))
        if new_qty < 1:
            return None

    return dataclasses.replace(draft, limit_price=new_limit, qty=new_qty, anchor_method=new_anchor)


def revise_node(state: SuggestionReviewState) -> SuggestionReviewState:
    """Apply critic decisions. Pure Python, no LLM calls."""
    decisions = state["critic_decisions"]
    ctx = state["context"]
    finals: list[OrderSuggestionRow] = []

    for i, draft in enumerate(state["drafts"]):
        dec = decisions.get(i)
        if dec is None or dec.verdict == "approve":
            finals.append(draft)
            continue

        if dec.verdict == "reject":
            log.info("critic rejected draft %s/%s: %s", i, draft.ticker, dec.reason)
            continue

        # revise: apply suggested_changes
        changes = dec.suggested_changes or {}
        revised = _apply_changes(draft, changes, ctx)
        if revised is None:
            log.warning(
                "critic asked for invalid changes on draft %s: %r — keeping original",
                i,
                changes,
            )
            finals.append(draft)  # keep original on bad changes
            continue

        finals.append(revised)

    return {**state, "finals": finals}


# ---------------------------------------------------------------------------
# Finalize node
# ---------------------------------------------------------------------------


def finalize_node(
    state: SuggestionReviewState,
    session_factory: Any,
) -> SuggestionReviewState:
    """Persist finals to the DB inside a session scope; capture IDs for magic links."""
    with session_factory() as s:
        ids = persist_suggestions(s, state["finals"], state["targets_id"], state["week_of"])
    return {**state, "suggestion_ids": ids}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def build_suggestion_review_graph(
    llm: Any,
    session_factory: Any,
    watchlist: list[str],
    bars_dir: str,
    settings: Any,
    earnings_client: Any,
) -> Any:
    """Build and compile the suggestion review graph.

    gather_context → reason → context_adjust → critic → revise/skip_revise → finalize

    Nodes are bound at compile time via lambdas — ``llm``, ``session_factory``,
    ``watchlist``, ``bars_dir``, ``settings``, and ``earnings_client`` are closed
    over rather than passed through LangGraph's configurable config (simpler than
    news_triage pattern since we don't need per-invoke session injection).

    Example invoke::

        result = graph.invoke(
            {
                "week_of": date.today(),
                "drafts": suggestions,
                "rationales": {},
                "critic_decisions": {},
                "finals": [],
                "telemetry": {},
                "targets_id": None,
                # context populated by gather_context_node
            },
            config={"configurable": {"thread_id": f"review-{date.today()}"}},
        )
    """
    checkpointer = make_checkpointer()

    g: StateGraph = StateGraph(SuggestionReviewState)

    g.add_node(
        "gather_context",
        lambda s: gather_context_node(
            s, session_factory, watchlist, bars_dir, settings, earnings_client
        ),
    )
    g.add_node("reason", lambda s: reason_node(s, llm, session_factory))
    g.add_node("context_adjust", lambda s: context_adjust_node(s, llm, session_factory, settings))
    g.add_node("critic", lambda s: critic_node(s, llm, session_factory, settings))
    g.add_node("revise", revise_node)
    g.add_node("skip_revise", skip_revise_node)
    g.add_node("finalize", lambda s: finalize_node(s, session_factory))

    g.add_edge(START, "gather_context")
    g.add_edge("gather_context", "reason")
    g.add_edge("reason", "context_adjust")
    g.add_edge("context_adjust", "critic")
    g.add_conditional_edges(
        "critic",
        route_after_critic,
        {"revise": "revise", "skip_revise": "skip_revise"},
    )
    g.add_edge("revise", "finalize")
    g.add_edge("skip_revise", "finalize")
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer)
