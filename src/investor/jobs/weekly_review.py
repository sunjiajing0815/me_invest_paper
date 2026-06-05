"""Friday weekly review email — 7-section reflection on the past week."""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..brokers.base import BrokerAdapter
from ..config import Settings, load_targets
from ..db import session_scope
from ..models import (
    AutoTradePromotionLog,
    BrokerAccount,
    KillSwitchLog,
    OrderExecution,
    OrderSuggestion,
)
from ..services.accounts import (
    AccountInfo,
    list_active_accounts,
    resolve_primary_account_ref,
)
from ..services.auto_trade import _get_mode
from ..services.daily_report import AccountSnapshot
from ..services.email import EmailSender
from ..services.gap import GapRow, compute_gap
from ..services.indicators import compute_indicators
from ..services.levels import build_nearby_levels, compute_levels
from ..services.llm import LLMClient
from ..services.news import load_recent_material_news
from ..services.render import render_template
from ..services.sentiment import SentimentClient
from ..services.snapshot import take_snapshot
from ..services.suggest import HALF_THE_GAP, _next_monday, generate_suggestions
from ..services.targets import targets_path_for_account
from ..services.tavily import TavilyClient
from ..services.weekly_context import (
    WeeklyMarketContext,
    build_weekly_market_context,
    persist_weekly_context,
)
from ..services.weekly_review_metrics import (
    AllocationDriftRow,
    OrderFlow,
    OrderFunnel,
    PerTickerWeekRow,
    WeekTrendRow,
    compute_4_week_trend,
    compute_allocation_drift,
    compute_order_flow,
    compute_order_funnel,
    compute_per_ticker_breakdown,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SuggestionAudit:
    """Plain-data suggestion audit row — safe after session closes."""

    ticker: str
    side: str
    qty: float
    limit_price: float
    status: str           # pending|accepted|rejected|expired|filled
    acted_at: datetime | None
    filled_price: float | None
    fill_status: str | None  # from order_execution.status, or None if no fill


@dataclass(frozen=True)
class WeeklyReview:
    """All data for the Friday weekly review email. Plain types only — no ORM objects."""

    week_of: date
    account: AccountSnapshot | None
    realized_pnl_usd: float
    suggestion_audits: list[SuggestionAudit]
    gap_rows: list[GapRow]
    material_news: dict[str, list[dict[str, str | None]]]  # ticker -> list of {sentiment, summary}
    auto_trade_mode: str
    promotions_this_week: list[dict[str, Any]]
    kill_switches_this_week: list[dict[str, Any]]
    executions_this_week: int
    preview_suggestions: list[dict[str, Any]]  # simplified dicts from generate_suggestions()
    moomoo_status: str  # "parallel_running" | "primary" | "unavailable"
    market_context: WeeklyMarketContext | None = None  # Phase 4.5: Tavily weekly digest
    order_funnel: OrderFunnel | None = None            # Phase 4.8: order activity metrics
    order_flow: OrderFlow | None = None
    drift_rows: list[AllocationDriftRow] | None = None
    breakdown_rows: list[PerTickerWeekRow] | None = None
    trend_rows: list[WeekTrendRow] | None = None


def _week_start(ref: date | None = None) -> datetime:
    """Return Monday 00:00 UTC of the current week."""
    d = ref or datetime.now(UTC).date()
    monday = d - timedelta(days=d.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=UTC)


def _build_review(
    *,
    session: Any,
    adapter: BrokerAdapter,
    settings: Settings,
    week_of: date,
    broker_account_id: int,
) -> WeeklyReview:
    """Build WeeklyReview from DB for ONE broker account. Caller inside session_scope()."""
    week_start = _week_start(week_of)

    # Account
    take_snapshot(adapter, session, settings, broker_account_id)
    orm_account = (
        session.query(BrokerAccount)
        .filter(
            BrokerAccount.account_ref == broker_account_id,
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
        )
        if orm_account else None
    )

    # Realized PnL this week
    pnl_rows = session.query(OrderExecution).filter(
        OrderExecution.broker_account_id == broker_account_id,
        OrderExecution.dry_run.is_(False),
        OrderExecution.side == "sell",
        OrderExecution.realized_pnl_usd.isnot(None),
        OrderExecution.filled_at >= week_start,
    ).all()
    realized_pnl_usd = sum(r.realized_pnl_usd for r in pnl_rows if r.realized_pnl_usd)

    # Suggestion audit
    suggestions_this_week = session.query(OrderSuggestion).filter(
        OrderSuggestion.broker_account_id == broker_account_id,
        OrderSuggestion.week_of == week_of,
    ).all()
    suggestion_audits = []
    now = datetime.now(UTC)
    for s in suggestions_this_week:
        # Find matching fill (by suggestion_id FK on OrderExecution)
        fill = session.query(OrderExecution).filter(
            OrderExecution.suggestion_id == s.id,
            OrderExecution.dry_run.is_(False),
        ).first()
        audit_status = s.status
        if s.status == "pending" and s.expires_at is not None and s.expires_at <= now:
            audit_status = "pending (expires Mon)"
        suggestion_audits.append(SuggestionAudit(
            ticker=s.ticker,
            side=s.side,
            qty=s.qty,
            limit_price=s.limit_price,
            status=audit_status,
            acted_at=s.acted_at,
            filled_price=fill.filled_price if fill else None,
            fill_status=fill.status if fill else None,
        ))

    # Drift state
    gap_rows = compute_gap(session, broker_account_id)

    # Material news this week (7 days back)
    material_news_raw = load_recent_material_news(session, days=7)
    material_news: dict[str, list[dict[str, str | None]]] = {
        ticker: [{"sentiment": item.sentiment, "summary": item.summary} for item in items]
        for ticker, items in material_news_raw.items()
    }

    # Auto-trade summary — per-account mode from auto_trade_state (the weekly review is
    # primary-scoped). NOT the meta.auto_trade_mode key, which migration d8589 deleted —
    # reading it always returned OFF regardless of the real mode.
    auto_trade_mode = _get_mode(session, broker_account_id)

    promotions = session.query(AutoTradePromotionLog).filter(
        AutoTradePromotionLog.ts >= week_start
    ).order_by(AutoTradePromotionLog.ts).all()
    promotions_this_week = [
        {
            "ts": str(p.ts),
            "from_mode": p.from_mode,
            "to_mode": p.to_mode,
            "broker_scope": p.broker_scope,
            "reason": p.reason,
            "actor": p.actor,
        }
        for p in promotions
    ]

    kill_switches = session.query(KillSwitchLog).filter(
        KillSwitchLog.ts >= week_start
    ).order_by(KillSwitchLog.ts).all()
    kill_switches_this_week = [
        {"ts": str(k.ts), "trigger": k.trigger, "detail": k.detail}
        for k in kill_switches
    ]

    executions_this_week = session.query(OrderExecution).filter(
        OrderExecution.broker_account_id == broker_account_id,
        OrderExecution.dry_run.is_(False),
        OrderExecution.filled_at >= week_start,
    ).count()

    # Moomoo status
    moomoo_status = "unavailable"
    if settings.broker == "moomoo":
        moomoo_status = "primary"
    elif settings.opend_host:
        moomoo_status = "parallel_running"

    # Phase 4.8: order activity metrics
    fri = week_of + timedelta(days=4)
    order_funnel: OrderFunnel | None = None
    order_flow: OrderFlow | None = None
    drift_rows: list[AllocationDriftRow] | None = None
    breakdown_rows: list[PerTickerWeekRow] | None = None
    trend_rows: list[WeekTrendRow] | None = None
    try:
        order_funnel = compute_order_funnel(
            session, mon=week_of, fri=fri, broker_account_id=broker_account_id
        )
        order_flow = compute_order_flow(
            session, mon=week_of, fri=fri, broker_account_id=broker_account_id
        )
        drift_rows = compute_allocation_drift(
            session, mon=week_of, fri=fri, broker_account_id=broker_account_id
        )
        breakdown_rows = compute_per_ticker_breakdown(
            session,
            mon=week_of,
            fri=fri,
            broker_account_id=broker_account_id,
            top_n=settings.weekly_review_breakdown_top_n,
        )
        trend_rows = compute_4_week_trend(
            session,
            current_fri=fri,
            broker_account_id=broker_account_id,
            trend_weeks=settings.weekly_review_trend_weeks,
        )
    except Exception as exc:
        logger.warning("_build_review: order activity metrics failed, omitting section: %s", exc)

    return WeeklyReview(
        week_of=week_of,
        account=account,
        realized_pnl_usd=realized_pnl_usd,
        suggestion_audits=suggestion_audits,
        gap_rows=gap_rows,
        material_news=material_news,
        auto_trade_mode=auto_trade_mode,
        promotions_this_week=promotions_this_week,
        kill_switches_this_week=kill_switches_this_week,
        executions_this_week=executions_this_week,
        preview_suggestions=[],  # populated below outside session scope
        moomoo_status=moomoo_status,
        order_funnel=order_funnel,
        order_flow=order_flow,
        drift_rows=drift_rows,
        breakdown_rows=breakdown_rows,
        trend_rows=trend_rows,
    )


def _build_and_persist_context(
    settings: Settings,
    llm: LLMClient,
    tavily: TavilyClient,
    watchlist: list[str],
    week_of: date,
    sentiment_client: SentimentClient | None,
) -> WeeklyMarketContext | None:
    """Build the USER-LEVEL weekly market context once and persist it keyed to the upcoming
    Monday (so the Sunday suggestion engine's loader finds it). One synthesis serves every
    account's review — never rebuild it per account (ADR-0020). Returns None on failure.

    Wrapped in a short session scope so the synthesis LLM call lands in llm_call_log; the
    SQLite write lock is taken only at the final flush, not during the Tavily/LLM I/O.
    """
    try:
        with session_scope() as ctx_session:
            market_context = build_weekly_market_context(
                tavily=tavily,
                llm=llm,
                session=ctx_session,
                watchlist=watchlist,
                week_of=week_of,
                prompt_version=settings.weekly_context_prompt_version,
                sentiment_client=sentiment_client,
            )
    except Exception as exc:
        logger.warning(
            "weekly market context failed; omitting section: %s", exc, exc_info=True
        )
        return None

    if market_context is not None:
        persist_week_of = _next_monday()
        try:
            with session_scope() as s:
                persist_weekly_context(
                    s, dataclasses.replace(market_context, week_of=persist_week_of)
                )
                s.commit()
        except Exception as exc:
            logger.warning("failed to persist weekly context: %s", exc)
    return market_context


def run_weekly_review_for_account(
    settings: Settings,
    adapter: BrokerAdapter,
    emailer: EmailSender,
    *,
    account: AccountInfo,
    primary_ref: int | None,
    week_of: date,
    market_context: WeeklyMarketContext | None,
) -> None:
    """Build + render + email the weekly review for ONE broker account.

    The user-level ``market_context`` is built once by the caller and passed in (it must NOT
    be rebuilt per account). Subject is prefixed ``[nickname]``. Re-raises on email failure
    (ADR-0005).
    """
    bid = account.account_ref
    targets_path = targets_path_for_account(settings, bid, is_primary=(bid == primary_ref))
    tickers = load_targets(targets_path).watchlist if targets_path else []

    with session_scope() as session:
        review = _build_review(
            session=session,
            adapter=adapter,
            settings=settings,
            week_of=week_of,
            broker_account_id=bid,
        )

    # Next-Sunday preview — pure function, no DB writes; outside the review session scope.
    try:
        indicators = compute_indicators(tickers, settings.bars_dir)
        sr_rows = compute_levels(tickers, indicators, settings.bars_dir)
        with session_scope() as session:
            gap_rows_fresh = compute_gap(session, bid)
            orm_acct = (
                session.query(BrokerAccount)
                .filter(
                    BrokerAccount.account_ref == bid,
                    BrokerAccount.effective_to.is_(None),
                )
                .order_by(BrokerAccount.last_sync.desc())
                .first()
            )
            preview_account = (
                AccountSnapshot(
                    broker=orm_acct.broker,
                    mode=orm_acct.mode,
                    cash_usd=orm_acct.cash_usd,
                    equity_usd=orm_acct.equity_usd,
                )
                if orm_acct else AccountSnapshot("unknown", "unknown", 0.0, 0.0)
            )
            nearby = build_nearby_levels(tickers, sr_rows, indicators)
        drafts, _ = generate_suggestions(
            gap_rows=gap_rows_fresh,
            nearby_levels=nearby,
            account=preview_account,
            sizing_rule=HALF_THE_GAP,
        )
        preview_suggestions = [
            {
                "ticker": d.ticker,
                "side": d.side,
                "qty": d.qty,
                "limit_price": d.limit_price,
                "reason": d.reason,
            }
            for d in drafts
        ]
    except Exception as exc:
        logger.warning(
            "weekly_review preview generation failed for %s: %s", account.nickname, exc
        )
        preview_suggestions = []

    # Rebuild review with preview + market_context (frozen dataclass — new instance)
    review = WeeklyReview(
        week_of=review.week_of,
        account=review.account,
        realized_pnl_usd=review.realized_pnl_usd,
        suggestion_audits=review.suggestion_audits,
        gap_rows=review.gap_rows,
        material_news=review.material_news,
        auto_trade_mode=review.auto_trade_mode,
        promotions_this_week=review.promotions_this_week,
        kill_switches_this_week=review.kill_switches_this_week,
        executions_this_week=review.executions_this_week,
        preview_suggestions=preview_suggestions,
        moomoo_status=review.moomoo_status,
        market_context=market_context,
        order_funnel=review.order_funnel,
        order_flow=review.order_flow,
        drift_rows=review.drift_rows,
        breakdown_rows=review.breakdown_rows,
        trend_rows=review.trend_rows,
    )

    subject = f"[{account.nickname}] Weekly review: {week_of:%b %d, %Y}"
    html = render_template("weekly_review.html.j2", review=review)
    text = render_template("weekly_review.txt.j2", review=review)
    emailer.send(to=settings.email_to, subject=subject, html=html, text=text)
    logger.info(
        "weekly_review: email sent for %s week_of=%s, pnl=%.2f",
        account.nickname, week_of, review.realized_pnl_usd,
    )


def run_weekly_review(
    settings: Settings,
    adapter: BrokerAdapter,
    emailer: EmailSender,
    llm: LLMClient,
    tavily: TavilyClient,
    sentiment_client: SentimentClient | None = None,
    *,
    account: AccountInfo | None = None,
) -> None:
    """Single-account weekly review (defaults to the primary). Builds the user-level market
    context, then renders + emails the review. Pass ``account`` to target a specific broker
    account. Re-raises on email failure (ADR-0005).
    """
    today = datetime.now(UTC).date()
    if today.weekday() not in {4, 5, 6}:  # 4=Fri, 5=Sat, 6=Sun
        raise RuntimeError(
            f"run_weekly_review: called on {today.strftime('%A')} — week isn't over yet"
        )
    week_of = _next_monday() - timedelta(days=7)  # last Monday (the week just ended)

    with session_scope() as session:
        primary_ref = resolve_primary_account_ref(session)
        accounts = list_active_accounts(session)
    if account is None:
        account = next((a for a in accounts if a.account_ref == primary_ref), None)
    if account is None:
        logger.warning("run_weekly_review: no active broker account — skipping")
        return

    targets_path = targets_path_for_account(
        settings, account.account_ref, is_primary=(account.account_ref == primary_ref)
    )
    tickers = load_targets(targets_path).watchlist if targets_path else []
    market_context = _build_and_persist_context(
        settings, llm, tavily, tickers, week_of, sentiment_client
    )
    run_weekly_review_for_account(
        settings, adapter, emailer,
        account=account, primary_ref=primary_ref, week_of=week_of,
        market_context=market_context,
    )


def run_weekly_review_all_brokers(
    settings: Settings,
    emailer: EmailSender,
    llm: LLMClient,
    tavily: TavilyClient,
    adapters: dict[int, BrokerAdapter],
    sentiment_client: SentimentClient | None = None,
) -> None:
    """Weekly review for every active broker account — one email each, ``[nickname]``
    subjects. The user-level market context is built ONCE (union of all watchlists) and
    shared across accounts. One account's failure is logged; the rest continue.
    """
    today = datetime.now(UTC).date()
    if today.weekday() not in {4, 5, 6}:  # 4=Fri, 5=Sat, 6=Sun
        raise RuntimeError(
            f"run_weekly_review_all_brokers: called on {today.strftime('%A')} —"
            " week isn't over yet"
        )
    week_of = _next_monday() - timedelta(days=7)

    with session_scope() as session:
        accounts = list_active_accounts(session)
        primary_ref = resolve_primary_account_ref(session)
    if not accounts:
        logger.warning("run_weekly_review_all_brokers: no active broker account — skipping")
        return

    # Union of all active watchlists → one Tavily fanout covers every account's tickers.
    union_watchlist: list[str] = []
    seen: set[str] = set()
    for acct in accounts:
        tp = targets_path_for_account(
            settings, acct.account_ref, is_primary=(acct.account_ref == primary_ref)
        )
        if tp is None:
            continue
        for t in load_targets(tp).watchlist:
            if t not in seen:
                seen.add(t)
                union_watchlist.append(t)

    market_context = _build_and_persist_context(
        settings, llm, tavily, union_watchlist, week_of, sentiment_client
    )

    for account in accounts:
        adapter = adapters.get(account.account_ref)
        if adapter is None:
            logger.warning(
                "weekly_review: no adapter for account_ref=%s (%s); skipping",
                account.account_ref, account.nickname,
            )
            continue
        try:
            run_weekly_review_for_account(
                settings, adapter, emailer,
                account=account, primary_ref=primary_ref, week_of=week_of,
                market_context=market_context,
            )
        except Exception:
            logger.exception(
                "weekly_review failed for account_ref=%s (%s); continuing",
                account.account_ref, account.nickname,
            )
            continue
