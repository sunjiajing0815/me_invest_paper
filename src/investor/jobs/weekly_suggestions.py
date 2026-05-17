"""Weekly suggestions job: compute indicators + levels, generate suggestions, email."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import pydantic

from ..brokers.base import BrokerAdapter
from ..config import Settings, load_targets
from ..db import session_scope
from ..graphs.suggestion_review import build_suggestion_review_graph
from ..models import BrokerAccount
from ..services.bars import update_bars
from ..services.daily_report import AccountSnapshot
from ..services.email import EmailSender
from ..services.gap import compute_gap, get_untracked_positions
from ..services.indicators import compute_indicators
from ..services.levels import (
    build_nearby_levels,
    compute_levels,
    get_active_targets_id,
    persist_levels,
)
from ..services.llm import LLMClient
from ..services.llm_levels import ScoredLevel, score_levels_for_ticker
from ..services.magic_link import sign_action
from ..services.news import load_recent_material_news
from ..services.render import render_template
from ..services.snapshot import take_snapshot
from ..services.suggest import (
    HALF_THE_GAP,
    _next_monday,
    generate_suggestions,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoringFailure:
    ticker: str
    exc_type: str
    exc_message: str


def score_all_tickers_parallel(
    *,
    tickers: list[str],
    sr_rows: list,  # list[SRLevelRow]
    llm: LLMClient,
    bars_dir: str,
    recent_news_by_ticker: dict,
    prompt_version: str,
    max_workers: int = 4,
) -> tuple[dict[str, list[ScoredLevel]], list[ScoringFailure]]:
    """Score S/R levels for all tickers in parallel via ThreadPoolExecutor.

    Each worker opens its own session so there's no session sharing across threads.
    Falls back to [] for any ticker that raises. max_workers=4 is conservative;
    Anthropic rate limits are tighter during bursts — tune up if no 429s observed.
    Returns (scored_map, failures) so callers can surface parse/validation errors.
    """
    out: dict[str, list[ScoredLevel]] = {}
    failures: list[ScoringFailure] = []

    def _score_one(ticker: str) -> list[ScoredLevel]:
        ticker_levels = [r for r in sr_rows if r.ticker == ticker]
        ticker_news = [
            {"sentiment": n.sentiment or "", "summary": n.summary or ""}
            for n in recent_news_by_ticker.get(ticker, [])
            if n.is_material
        ]
        with session_scope() as session:
            return score_levels_for_ticker(
                llm=llm,
                session=session,
                ticker=ticker,
                computed_levels=ticker_levels,
                bars_dir=bars_dir,
                recent_news=ticker_news or None,
                prompt_version=prompt_version,
            )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_score_one, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                out[ticker] = fut.result()
            except (json.JSONDecodeError, pydantic.ValidationError) as exc:
                logger.warning("level scoring failed for %s: %s", ticker, exc, exc_info=True)
                out[ticker] = []
                failures.append(
                    ScoringFailure(
                        ticker=ticker,
                        exc_type=type(exc).__name__,
                        exc_message=str(exc)[:200],
                    )
                )
            except Exception as exc:
                logger.exception("level scoring failed for %s: %s", ticker, exc)
                out[ticker] = []
                failures.append(
                    ScoringFailure(
                        ticker=ticker,
                        exc_type=type(exc).__name__,
                        exc_message=str(exc)[:200],
                    )
                )

    return out, failures


def run_weekly_suggestions(
    settings: Settings, adapter: BrokerAdapter, emailer: EmailSender, llm: LLMClient
) -> None:
    """Compute indicators + levels, generate suggestions, review via graph, and email.

    Order of operations:
      1. update_bars (tolerates failure — stale bars are better than no email)
      2. compute_indicators (DuckDB, no session needed)
      3. LLM level scoring (separate session scope)
      4. snapshot + gap + levels inside session scope; generate drafts (no persist yet)
      5. suggestion-review graph: gather_context → reason → critic → revise → finalize
         finalize_node persists finals and returns suggestion_ids
      6. render + email outside session scope
    Re-raises on email failure (matches ADR-0005).
    """
    logger.info("run_weekly_suggestions started")

    targets = load_targets(settings.targets_path)
    tickers = targets.watchlist

    try:
        update_bars(
            tickers=tickers,
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            bars_dir=settings.bars_dir,
        )
    except Exception as exc:
        logger.warning("update_bars failed; continuing with stale bars: %s", exc)

    indicators = compute_indicators(tickers, settings.bars_dir)
    sr_rows = compute_levels(tickers, indicators, settings.bars_dir)
    week_of = _next_monday()

    # Load last-24h material news, then score all tickers in parallel
    with session_scope() as session:
        recent_news_by_ticker = load_recent_material_news(session, days=1)

    scored, scoring_failures = score_all_tickers_parallel(
        tickers=tickers,
        sr_rows=sr_rows,
        llm=llm,
        bars_dir=settings.bars_dir,
        recent_news_by_ticker=recent_news_by_ticker,
        prompt_version=settings.level_prompt_version,
    )

    # Generate drafts (pure function — no persist yet)
    nearby: dict  # type: ignore[type-arg]
    account: AccountSnapshot
    targets_id: int | None
    with session_scope() as session:
        take_snapshot(adapter, session, settings)
        gap_rows = compute_gap(session)

        orm_account = (
            session.query(BrokerAccount)
            .filter(BrokerAccount.effective_to.is_(None))
            .order_by(BrokerAccount.last_sync.desc())
            .first()
        )
        account = (
            AccountSnapshot(
                broker=orm_account.broker,
                mode=orm_account.mode,
                cash_usd=orm_account.cash_usd,
                equity_usd=orm_account.equity_usd,
            )
            if orm_account is not None
            else AccountSnapshot(broker="unknown", mode="unknown", cash_usd=0.0, equity_usd=0.0)
        )

        targets_id = get_active_targets_id(session)
        persist_levels(session, sr_rows)

        nearby = build_nearby_levels(tickers, sr_rows, indicators)
        drafts, skipped = generate_suggestions(
            gap_rows=gap_rows,
            nearby_levels=nearby,
            account=account,
            sizing_rule=HALF_THE_GAP,
            scored_levels=scored,
        )
        # NOTE: do NOT call persist_suggestions here — finalize_node does it.

    # Suggestion review graph: reason → critic → (revise) → finalize
    # finalize_node persists finals and writes suggestion_ids to state.
    graph = build_suggestion_review_graph(
        llm=llm,
        session_factory=session_scope,
        watchlist=tickers,
        bars_dir=settings.bars_dir,
    )
    result = graph.invoke(
        {
            "week_of": week_of,
            "drafts": drafts,
            "context": None,
            "rationales": {},
            "critic_decisions": {},
            "finals": [],
            "suggestion_ids": [],
            "telemetry": {},
            "targets_id": targets_id,
        },
        config={"configurable": {"thread_id": f"weekly-{week_of}"}},
    )

    finals = result["finals"]
    rationales: dict[int, str] = result["rationales"]
    suggestion_ids: list[int] = result["suggestion_ids"]

    # Build token context list for Accept/Reject buttons
    suggestion_items = []
    for idx, (suggestion, sid) in enumerate(zip(finals, suggestion_ids, strict=False)):
        accept_token = sign_action(sid, "accept", settings.magic_link_secret)
        reject_token = sign_action(sid, "reject", settings.magic_link_secret)
        suggestion_items.append({
            "suggestion": suggestion,
            "sid": sid,
            "rationale": rationales.get(idx, suggestion.reason),
            "accept_token": accept_token,
            "reject_token": reject_token,
        })

    # Untracked positions for email (re-query so we don't hold session across graph)
    with session_scope() as session:
        untracked = get_untracked_positions(session)

    # Email outside session scope — session safety rule
    subject = f"Orders for the week of {week_of:%b %d}"
    html = render_template(
        "weekly_suggestions.html.j2",
        week_of=week_of,
        account=account,
        suggestion_items=suggestion_items,
        base_url=settings.app_base_url,
        indicators=indicators,
        nearby=nearby,
        untracked=untracked,
        skipped=skipped,
        scoring_failures=scoring_failures,
    )
    text = render_template(
        "weekly_suggestions.txt.j2",
        week_of=week_of,
        account=account,
        suggestions=finals,
        indicators=indicators,
        nearby=nearby,
        untracked=untracked,
        skipped=skipped,
        scoring_failures=scoring_failures,
    )
    emailer.send(to=settings.email_to, subject=subject, html=html, text=text)
    logger.info(
        "run_weekly_suggestions completed: %d suggestions emailed to %s",
        len(finals),
        settings.email_to,
    )
