"""Weekly suggestions job: compute indicators + levels, generate suggestions, email."""

from __future__ import annotations

import dataclasses
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pydantic
from sqlalchemy import select

from ..brokers.base import BrokerAdapter
from ..config import Settings, load_targets
from ..db import session_scope
from ..graphs.news_triage import NewsTriageItem
from ..graphs.suggestion_review import build_suggestion_review_graph
from ..models import BrokerAccount, OrderSuggestion
from ..services.accounts import (
    AccountInfo,
    list_active_accounts,
    resolve_primary_account_ref,
)
from ..services.bars import update_bars
from ..services.daily_report import AccountSnapshot, _account_currency
from ..services.earnings import EarningsWarning, build_earnings_warnings
from ..services.email import EmailSender
from ..services.gap import compute_gap, get_untracked_positions
from ..services.indicators import compute_indicators
from ..services.levels import (
    SRLevelRow,
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
    SkippedRow,
    _next_monday,
    generate_suggestions,
    generate_topup_suggestions,
    topup_size_fraction,
)
from ..services.targets import targets_path_for_account
from ..services.ticker_names import names_for
from ..services.weekly_context import load_latest_weekly_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoringFailure:
    ticker: str
    exc_type: str
    exc_message: str


def score_all_tickers_parallel(
    *,
    tickers: list[str],
    sr_rows: list[SRLevelRow],
    llm: LLMClient,
    bars_dir: str,
    recent_news_by_ticker: dict[str, list[NewsTriageItem]],
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


def run_weekly_suggestions_for_account(
    settings: Settings,
    adapter: BrokerAdapter,
    emailer: EmailSender,
    llm: LLMClient,
    earnings_client: Any,
    acct: AccountInfo,
) -> None:
    """Compute indicators + levels, generate suggestions, review via graph, and email —
    for ONE broker account (all DB reads/writes scoped by acct.account_ref).

    Order of operations:
      1. update_bars (tolerates failure — stale bars are better than no email)
      2. compute_indicators (DuckDB, no session needed)
      3. persist_levels (MUST precede scoring — the score write-back updates these rows)
      4. LLM level scoring (per-worker session scopes)
      5. snapshot + gap inside session scope; generate drafts (no persist yet)
      5. suggestion-review graph: gather_context → reason → critic → revise → finalize
         finalize_node persists finals and returns suggestion_ids
      6. render + email outside session scope
    Re-raises on email failure (matches ADR-0005).
    """
    bid = acct.account_ref
    logger.info(
        "run_weekly_suggestions started for %s (account_ref=%s)", acct.nickname, bid
    )

    with session_scope() as session:
        primary_ref = resolve_primary_account_ref(session)
    targets_path = targets_path_for_account(settings, bid, is_primary=bid == primary_ref)
    if targets_path is None:
        logger.warning(
            "run_weekly_suggestions: no targets file for %s (account_ref=%s); skipping",
            acct.nickname, bid,
        )
        return
    targets = load_targets(targets_path)
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

    # Persist computed levels BEFORE scoring: score_levels_for_ticker writes confidences
    # back onto sr_level rows by (ticker, method, as_of) — if the rows don't exist yet the
    # write-back silently no-ops and the DB keeps serving stale scored sets to the review
    # graph (the MU $751.43 bug: last persisted scores were 6 weeks old). The upsert only
    # touches price/type on conflict, so re-running never clobbers scores.
    with session_scope() as session:
        persist_levels(session, sr_rows)

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
        take_snapshot(adapter, session, settings, bid)
        gap_rows = compute_gap(session, bid)

        orm_account = (
            session.query(BrokerAccount)
            .filter(
                BrokerAccount.account_ref == bid,
                BrokerAccount.effective_to.is_(None),
            )
            .order_by(BrokerAccount.last_sync.desc())
            .first()
        )
        account = (
            AccountSnapshot(
                broker=orm_account.broker,
                mode=orm_account.mode,
                cash_usd=orm_account.cash_usd,
                equity_usd=orm_account.equity_usd,
                currency=_account_currency(orm_account.connection_config),
            )
            if orm_account is not None
            else AccountSnapshot(broker="unknown", mode="unknown", cash_usd=0.0, equity_usd=0.0)
        )

        targets_id = get_active_targets_id(session)

        nearby = build_nearby_levels(tickers, sr_rows, indicators, bars_dir=settings.bars_dir)
        drafts, skipped = generate_suggestions(
            gap_rows=gap_rows,
            nearby_levels=nearby,
            account=account,
            sizing_rule=HALF_THE_GAP,
            scored_levels=scored,
        )
        # NOTE: do NOT call persist_suggestions here — finalize_node does it.

    # Top-up drafts (plans/pre_phase5_features_design.md): sentiment-sized near-target buys
    # for tickers below target that got no regular buy draft. Sized at creation from the
    # Friday-persisted VIX/F&G (context_adjust exempts kind='topup' — no double-count).
    if settings.topup_enabled:
        with session_scope() as session:
            _ctx = load_latest_weekly_context(
                session, week_of=week_of, max_age_days=settings.context_max_age_days
            )
            _news_7d = load_recent_material_news(session, days=7)
        bearish_7d = {
            t for t, items in _news_7d.items()
            if any(n.is_material and n.sentiment == "bearish" for n in items)
        }
        fraction = topup_size_fraction(
            _ctx.vix if _ctx else None, _ctx.fear_greed_score if _ctx else None
        )
        if _ctx and _ctx.fear_greed_score is not None:
            sentiment_note = f"fear&greed={_ctx.fear_greed_score}"
        elif _ctx and _ctx.vix is not None:
            sentiment_note = f"vix={_ctx.vix:.0f}"
        else:
            sentiment_note = "no sentiment data"
        spent = sum(d.qty * d.limit_price for d in drafts if d.side == "buy")
        topups = generate_topup_suggestions(
            gap_rows=gap_rows,
            nearby_levels=nearby,
            account=account,
            band_high_by_ticker={t.ticker: t.band_high for t in targets.targets},
            regular_buy_tickers={d.ticker for d in drafts if d.side == "buy"},
            sentiment_fraction=fraction,
            sentiment_note=sentiment_note,
            cash_available=account.cash_usd - spent,
            scored_levels=scored,
        )
        # Deterministic highlight: strong anchor confidence AND no bearish news in 7d.
        topups = [
            dataclasses.replace(t, is_highlighted=(
                t.confidence_at_creation is not None
                and t.confidence_at_creation >= settings.topup_highlight_min_conf
                and t.ticker not in bearish_7d
            ))
            for t in topups
        ]
        if topups:
            logger.info(
                "run_weekly_suggestions: %d top-up draft(s) ×%.2g (%s): %s",
                len(topups), fraction, sentiment_note,
                ", ".join(f"{t.ticker} x{t.qty:.0f}" for t in topups),
            )
        drafts = drafts + topups

    # Load cached LLM rationales for this week_of — keyed by (ticker, side).
    # reason_node will skip drafts whose index already has a rationale in state.
    with session_scope() as session:
        cached_rows = session.scalars(
            select(OrderSuggestion).where(
                OrderSuggestion.week_of == week_of,
                OrderSuggestion.broker_account_id == bid,
                OrderSuggestion.llm_rationale.is_not(None),
            )
        ).all()
        cached_rationales: dict[tuple[str, str], str] = {
            (r.ticker, r.side): r.llm_rationale  # type: ignore[misc]
            for r in cached_rows
        }

    pre_rationales = {
        i: cached_rationales[(d.ticker, d.side)]
        for i, d in enumerate(drafts)
        if (d.ticker, d.side) in cached_rationales
    }

    # Suggestion review graph: reason → critic → (revise) → finalize
    # finalize_node persists finals and writes suggestion_ids to state.
    graph = build_suggestion_review_graph(
        llm=llm,
        session_factory=session_scope,
        watchlist=tickers,
        bars_dir=settings.bars_dir,
        settings=settings,
        earnings_client=earnings_client,
    )
    result = graph.invoke(
        {
            "week_of": week_of,
            "broker_account_id": bid,
            "drafts": drafts,
            "context": None,
            "rationales": pre_rationales,
            "critic_decisions": {},
            "finals": [],
            "suggestion_ids": [],
            "telemetry": {},
            "targets_id": targets_id,
        },
        config={"configurable": {"thread_id": f"weekly-{bid}-{week_of}"}},
    )

    rationales: dict[int, str] = result["rationales"]
    suggestion_ids: list[int] = result["suggestion_ids"]

    # Surface critic-rejected drafts in the "Not Suggested" section so the email explains
    # why a ticker wasn't recommended (e.g. NFLX vetoed on bearish news). Reuse SkippedRow;
    # gap_pct comes from this run's gap_rows.
    gap_by_ticker = {g.ticker: g.gap_pct for g in gap_rows}
    for rej in result.get("rejections", []):
        skipped.append(SkippedRow(
            ticker=rej["ticker"],
            gap_pct=gap_by_ticker.get(rej["ticker"], 0.0),
            side=rej["side"],
            reason=f"review declined — {rej['reason']}",
        ))

    # Re-read persisted rows so the email always reflects what is in the DB.
    # state["finals"] can diverge for already-accepted suggestions (persist_suggestions
    # skips accepted rows, but the graph still computes fresh context-adjusted values
    # in-memory). Using DB values avoids emailing a qty that auto-trade won't honour.
    with session_scope() as _s:
        db_rows: dict[int, OrderSuggestion] = {
            sid: row
            for sid in suggestion_ids
            if (row := _s.get(OrderSuggestion, sid)) is not None
        }
        # Extract plain values before session closes (ORM safety rule)
        db_values: dict[int, dict[str, Any]] = {
            sid: {
                "ticker": r.ticker,
                "side": r.side,
                "qty": r.qty,
                "limit_price": r.limit_price,
                "reason": r.reason or "",
                "base_qty": r.base_qty,
                "size_factor": r.size_factor if r.size_factor is not None else 1.0,
                "context_note": r.context_note,
                "llm_rationale": r.llm_rationale,
                "kind": r.kind,
                "is_highlighted": r.is_highlighted,
            }
            for sid, r in db_rows.items()
        }

    # Build token context list for Accept/Reject buttons
    suggestion_items = []
    for idx, sid in enumerate(suggestion_ids):
        if sid not in db_values:
            continue
        v = db_values[sid]
        accept_token = sign_action(sid, "accept", settings.magic_link_secret)
        reject_token = sign_action(sid, "reject", settings.magic_link_secret)
        suggestion_items.append({
            "suggestion": v,
            "sid": sid,
            "rationale": v["llm_rationale"] or rationales.get(idx, v["reason"]),
            "accept_token": accept_token,
            "reject_token": reject_token,
        })

    regular_items = [i for i in suggestion_items if i["suggestion"].get("kind") != "topup"]
    topup_items = [i for i in suggestion_items if i["suggestion"].get("kind") == "topup"]

    # Earnings warnings: any watchlist ticker reporting this week or next. Reuses the
    # Finnhub earnings client (empty FINNHUB_API_KEY → no-op → no warning box).
    earnings_warnings: list[EarningsWarning] = []
    try:
        today = datetime.now(UTC).date()
        earnings_map = earnings_client.upcoming_earnings(
            tickers, start=today, end=week_of + timedelta(days=13)
        )
        suggested_now = {i["suggestion"]["ticker"] for i in suggestion_items}
        earnings_warnings = build_earnings_warnings(
            earnings_map,
            week_of=week_of,
            suggested_tickers=suggested_now,
            names=names_for(tickers),
            today=today,
        )
        if earnings_warnings:
            logger.info(
                "run_weekly_suggestions: %d earnings warning(s): %s",
                len(earnings_warnings),
                ", ".join(f"{w.ticker} {w.earnings_date}" for w in earnings_warnings),
            )
    except Exception as exc:  # never let an earnings-feed hiccup block the email
        logger.warning("run_weekly_suggestions: earnings warnings skipped: %s", exc)

    # Untracked positions + the user-level market context (VIX/F&G) for the email.
    with session_scope() as session:
        untracked = get_untracked_positions(session, bid)
        market_context = load_latest_weekly_context(
            session, week_of=week_of, max_age_days=settings.context_max_age_days
        )
    # MA200 in the email is shown for ETFs only.
    etf_tickers = {
        t.ticker for t in targets.targets
        if t.asset_class in ("index_etf", "leveraged_etf")
    }

    # Email outside session scope — session safety rule
    subject = f"[{acct.nickname}] Orders for the week of {week_of:%b %d}"
    html = render_template(
        "weekly_suggestions.html.j2",
        week_of=week_of,
        account=account,
        account_nickname=acct.nickname,
        account_broker=acct.broker,
        suggestion_items=regular_items,
        topup_items=topup_items,
        earnings_warnings=earnings_warnings,
        base_url=settings.app_base_url,
        indicators=indicators,
        nearby=nearby,
        untracked=untracked,
        skipped=skipped,
        scoring_failures=scoring_failures,
        market_context=market_context,
        etf_tickers=etf_tickers,
        ticker_names=names_for(tickers),
    )
    text = render_template(
        "weekly_suggestions.txt.j2",
        week_of=week_of,
        account=account,
        account_nickname=acct.nickname,
        account_broker=acct.broker,
        suggestions=list(db_values.values()),
        earnings_warnings=earnings_warnings,
        indicators=indicators,
        nearby=nearby,
        untracked=untracked,
        skipped=skipped,
        scoring_failures=scoring_failures,
        market_context=market_context,
        etf_tickers=etf_tickers,
    )
    emailer.send(to=settings.email_to, subject=subject, html=html, text=text)
    logger.info(
        "run_weekly_suggestions completed: %d suggestions emailed to %s",
        len(suggestion_items),
        settings.email_to,
    )


def run_weekly_suggestions_all_brokers(
    settings: Settings,
    emailer: EmailSender,
    llm: LLMClient,
    earnings_client: Any,
    adapters: dict[int, BrokerAdapter],
) -> None:
    """Run weekly suggestions for every active broker account, isolating failures."""
    with session_scope() as session:
        accounts = list_active_accounts(session)
    for acct in accounts:
        adapter = adapters.get(acct.account_ref)
        if adapter is None:
            logger.warning(
                "run_weekly_suggestions_all_brokers: no adapter for account_ref=%s (%s); skipping",
                acct.account_ref, acct.nickname,
            )
            continue
        try:
            run_weekly_suggestions_for_account(
                settings, adapter, emailer, llm, earnings_client, acct
            )
        except Exception:
            logger.exception(
                "weekly suggestions failed for account_ref=%s (%s); continuing",
                acct.account_ref, acct.nickname,
            )
            continue
