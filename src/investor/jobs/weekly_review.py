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
    Meta,
    OrderExecution,
    OrderSuggestion,
)
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
) -> WeeklyReview:
    """Build WeeklyReview from DB. Caller must be inside session_scope()."""
    week_start = _week_start(week_of)

    # Account
    take_snapshot(adapter, session, settings)
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
        if orm_account else None
    )

    # Realized PnL this week
    pnl_rows = session.query(OrderExecution).filter(
        OrderExecution.dry_run.is_(False),
        OrderExecution.side == "sell",
        OrderExecution.realized_pnl_usd.isnot(None),
        OrderExecution.filled_at >= week_start,
    ).all()
    realized_pnl_usd = sum(r.realized_pnl_usd for r in pnl_rows if r.realized_pnl_usd)

    # Suggestion audit
    suggestions_this_week = session.query(OrderSuggestion).filter(
        OrderSuggestion.week_of == week_of
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
    gap_rows = compute_gap(session)

    # Material news this week (7 days back)
    material_news_raw = load_recent_material_news(session, days=7)
    material_news: dict[str, list[dict[str, str | None]]] = {
        ticker: [{"sentiment": item.sentiment, "summary": item.summary} for item in items]
        for ticker, items in material_news_raw.items()
    }

    # Auto-trade summary
    meta = session.get(Meta, "auto_trade_mode")
    auto_trade_mode = meta.value if meta else "OFF"

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
        order_funnel = compute_order_funnel(session, mon=week_of, fri=fri)
        order_flow = compute_order_flow(session, mon=week_of, fri=fri)
        drift_rows = compute_allocation_drift(session, mon=week_of, fri=fri)
        breakdown_rows = compute_per_ticker_breakdown(
            session,
            mon=week_of,
            fri=fri,
            top_n=settings.weekly_review_breakdown_top_n,
        )
        trend_rows = compute_4_week_trend(
            session,
            current_fri=fri,
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


def run_weekly_review(
    settings: Settings,
    adapter: BrokerAdapter,
    emailer: EmailSender,
    llm: LLMClient,
    tavily: TavilyClient,
    sentiment_client: SentimentClient | None = None,
) -> None:
    """Build the weekly review data, render templates, and email.

    Re-raises on email failure to match ADR-0005 contract.
    """
    today = datetime.now(UTC).date()
    if today.weekday() not in {4, 5, 6}:  # 4=Fri, 5=Sat, 6=Sun
        raise RuntimeError(
            f"run_weekly_review: called on {today.strftime('%A')} — week isn't over yet"
        )

    targets = load_targets(settings.targets_path)
    tickers = targets.watchlist
    week_of = _next_monday() - timedelta(days=7)  # last Monday (the week just ended)

    with session_scope() as session:
        review = _build_review(
            session=session,
            adapter=adapter,
            settings=settings,
            week_of=week_of,
        )

    # Next-Sunday preview — pure function, no DB writes
    # Run outside session scope so the review session is fully closed first
    try:
        indicators = compute_indicators(tickers, settings.bars_dir)
        sr_rows = compute_levels(tickers, indicators, settings.bars_dir)
        with session_scope() as session:
            gap_rows_fresh = compute_gap(session)
            orm_acct = (
                session.query(BrokerAccount)
                .filter(BrokerAccount.effective_to.is_(None))
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
        logger.warning("run_weekly_review: preview generation failed: %s", exc)
        preview_suggestions = []

    # Phase 4.5: Tavily weekly market context (runs outside session scope)
    try:
        market_context = build_weekly_market_context(
            tavily=tavily,
            llm=llm,
            watchlist=tickers,
            week_of=week_of,
            prompt_version=settings.weekly_context_prompt_version,
            sentiment_client=sentiment_client,
        )
    except Exception as exc:
        logger.warning(
            "run_weekly_review: weekly market context failed; omitting section: %s",
            exc,
            exc_info=True,
        )
        market_context = None

    # Persist context keyed to the *upcoming* Monday so Sunday's loader finds it.
    if market_context is not None:
        persist_week_of = _next_monday()
        try:
            with session_scope() as s:
                persist_weekly_context(
                    s, dataclasses.replace(market_context, week_of=persist_week_of)
                )
                s.commit()
        except Exception as exc:
            logger.warning("run_weekly_review: failed to persist weekly context: %s", exc)

    # Rebuild review with preview + market_context (dataclass is frozen — create new instance)
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

    subject = f"Weekly review: {week_of:%b %d, %Y}"
    html = render_template("weekly_review.html.j2", review=review)
    text = render_template("weekly_review.txt.j2", review=review)
    emailer.send(to=settings.email_to, subject=subject, html=html, text=text)
    logger.info(
        "run_weekly_review: email sent for week_of=%s, pnl=%.2f",
        week_of, review.realized_pnl_usd,
    )
