"""Weekly suggestions job: compute indicators + levels, generate suggestions, email."""

from __future__ import annotations

import logging

from ..brokers.base import BrokerAdapter
from ..config import Settings, load_targets
from ..db import session_scope
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
from ..services.render import render_template
from ..services.snapshot import take_snapshot
from ..services.magic_link import sign_action
from ..services.suggest import (
    HALF_THE_GAP,
    _next_monday,
    generate_suggestions,
    persist_suggestions,
)

logger = logging.getLogger(__name__)


def run_weekly_suggestions(
    settings: Settings, adapter: BrokerAdapter, emailer: EmailSender, llm: LLMClient
) -> None:
    """Compute indicators + levels, generate suggestions, persist, and email.

    Order of operations:
      1. update_bars (tolerates failure — stale bars are better than no email)
      2. compute_indicators (DuckDB, no session needed)
      3. LLM level scoring (separate session scope)
      4. snapshot + gap + levels inside session scope
      5. generate suggestions (pure function)
      6. persist suggestions inside session scope
      7. render + email outside session scope
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

    # Score all levels per ticker via LLM
    scored: dict[str, list[ScoredLevel]] = {}
    with session_scope() as session:
        for ticker in tickers:
            ticker_levels = [r for r in sr_rows if r.ticker == ticker]
            try:
                scored[ticker] = score_levels_for_ticker(
                    llm=llm,
                    session=session,
                    ticker=ticker,
                    computed_levels=ticker_levels,
                    bars_dir=settings.bars_dir,
                )
            except Exception as exc:
                logger.warning("level scoring failed for %s: %s", ticker, exc)
                scored[ticker] = []

    with session_scope() as session:
        take_snapshot(adapter, session, settings)
        gap_rows = compute_gap(session)

        orm_account = (
            session.query(BrokerAccount)
            .filter(BrokerAccount.effective_to.is_(None))
            .order_by(BrokerAccount.last_sync.desc())
            .first()
        )
        account: AccountSnapshot = (
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
        suggestions = generate_suggestions(
            gap_rows=gap_rows,
            nearby_levels=nearby,
            account=account,
            sizing_rule=HALF_THE_GAP,
            scored_levels=scored,
        )

        suggestion_ids = persist_suggestions(session, suggestions, targets_id, week_of)
        untracked = get_untracked_positions(session)

    # Build token context list for Accept/Reject buttons
    suggestion_items = []
    for suggestion, sid in zip(suggestions, suggestion_ids):
        accept_token = sign_action(sid, "accept", settings.magic_link_secret)
        reject_token = sign_action(sid, "reject", settings.magic_link_secret)
        suggestion_items.append({
            "suggestion": suggestion,
            "sid": sid,
            "accept_token": accept_token,
            "reject_token": reject_token,
        })

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
    )
    text = render_template(
        "weekly_suggestions.txt.j2",
        week_of=week_of,
        account=account,
        suggestions=suggestions,
        indicators=indicators,
        nearby=nearby,
        untracked=untracked,
    )
    emailer.send(to=settings.email_to, subject=subject, html=html, text=text)
    logger.info(
        "run_weekly_suggestions completed: %d suggestions emailed to %s",
        len(suggestions),
        settings.email_to,
    )
