"""Auto-trade engine: place orders on behalf of accepted suggestions.

This is the ONLY module permitted to call adapter.submit_order() outside of
brokers/. See ADR-0014 and the grep CI test in tests/test_no_unauthorized_submit_order.py.

Mode lifecycle: OFF (default) → DRY_RUN (simulate) → LIVE (real orders).
Mode is stored in meta table key 'auto_trade_mode'.
Promotion requires soak-window enforcement (see POST /admin/auto-trade/promote).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..brokers.base import BrokerAdapter, OrderRequest
from ..models import (
    AutoTradeCaps,
    KillSwitchLog,
    Meta,
    OrderExecution,
    OrderSuggestion,
)
from ..services.email import EmailSender

logger = logging.getLogger(__name__)

Mode = Literal["OFF", "DRY_RUN", "LIVE"]
META_KEY_MODE = "auto_trade_mode"


@dataclass(frozen=True)
class AutoTradeOutcome:
    suggestion_id: int
    placed: bool
    dry_run: bool
    broker_order_id: str | None
    rejected_reason: str | None


class _GuardFailure(Exception):  # noqa: N818
    """Per-suggestion guard rejection. Does NOT trigger kill switch — just skips this suggestion."""


def _get_mode(session: Session) -> Mode:
    meta = session.get(Meta, META_KEY_MODE)
    if meta is None:
        return "OFF"
    val = meta.value.upper()
    if val in ("OFF", "DRY_RUN", "LIVE"):
        return val  # type: ignore[return-value]
    return "OFF"


def _get_active_caps(session: Session) -> AutoTradeCaps | None:
    return session.scalar(
        select(AutoTradeCaps)
        .where(AutoTradeCaps.effective_to.is_(None))
        .order_by(AutoTradeCaps.effective_from.desc())
    )


def _fetch_accepted_unexecuted(session: Session) -> list[OrderSuggestion]:
    """Return suggestions with status='accepted' that have no matching order_execution."""
    accepted = session.scalars(
        select(OrderSuggestion).where(OrderSuggestion.status == "accepted")
    ).all()
    result = []
    for sug in accepted:
        existing = session.scalar(
            select(OrderExecution).where(
                OrderExecution.client_order_id == f"sug-{sug.id}",
                OrderExecution.dry_run.is_(False),
            )
        )
        if existing is None:
            result.append(sug)
    return result


def _check_idempotency(session: Session, sug: OrderSuggestion, mode: Mode) -> None:
    """Raise _GuardFailure if a prior execution already exists for this suggestion."""
    dry_run_flag = mode == "DRY_RUN"
    existing = session.scalar(
        select(OrderExecution).where(
            OrderExecution.client_order_id == f"sug-{sug.id}",
            OrderExecution.dry_run.is_(dry_run_flag),
        )
    )
    if existing is not None:
        raise _GuardFailure(
            f"sug-{sug.id} already has an execution row (idempotency guard)"
        )


def _check_wash_sale(session: Session, sug: OrderSuggestion) -> None:
    """Raise _GuardFailure if a real loss-sell for this ticker occurred within 30 calendar days.

    dry_run=False filter is CRITICAL — simulated losses must never block real trades.
    """
    if sug.side != "buy":
        return
    cutoff = datetime.now(UTC) - timedelta(days=30)
    loss_sell = session.scalar(
        select(OrderExecution).where(
            OrderExecution.ticker == sug.ticker,
            OrderExecution.side == "sell",
            OrderExecution.dry_run.is_(False),
            OrderExecution.realized_pnl_usd < 0,
            OrderExecution.filled_at >= cutoff,
        )
    )
    if loss_sell is not None:
        raise _GuardFailure(
            f"wash-sale guard: loss sell on {sug.ticker} within 30 days"
            f" (filled_at={loss_sell.filled_at})"
        )


def _check_caps(
    session: Session,
    sug: OrderSuggestion,
    caps: AutoTradeCaps,
    mode: Mode,
) -> None:
    """Raise _GuardFailure if any spending cap would be breached."""
    order_cost = sug.qty * sug.limit_price

    # Per-order cap
    if order_cost > caps.per_order_max_usd:
        raise _GuardFailure(
            f"per-order cap: ${order_cost:,.0f} > ${caps.per_order_max_usd:,.0f}"
        )

    dry_run_flag = mode == "DRY_RUN"
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    # Per-day total spend
    day_spend = session.scalar(
        select(func.sum(OrderExecution.submitted_qty * OrderExecution.limit_price)).where(
            OrderExecution.dry_run.is_(dry_run_flag),
            OrderExecution.created_at >= today_start,
            OrderExecution.status != "rejected",
        )
    ) or 0.0
    if day_spend + order_cost > caps.per_day_max_usd:
        raise _GuardFailure(
            f"per-day cap: today ${day_spend:,.0f} + ${order_cost:,.0f}"
            f" > ${caps.per_day_max_usd:,.0f}"
        )

    # Per-week per-ticker spend
    ticker_week_spend = session.scalar(
        select(func.sum(OrderExecution.submitted_qty * OrderExecution.limit_price)).where(
            OrderExecution.ticker == sug.ticker,
            OrderExecution.dry_run.is_(dry_run_flag),
            OrderExecution.created_at >= week_start,
            OrderExecution.status != "rejected",
        )
    ) or 0.0
    if ticker_week_spend + order_cost > caps.per_week_max_usd_per_ticker:
        raise _GuardFailure(
            f"per-week-per-ticker cap: {sug.ticker} week ${ticker_week_spend:,.0f}"
            f" + ${order_cost:,.0f} > ${caps.per_week_max_usd_per_ticker:,.0f}"
        )

    # Per-day order count
    day_order_count = session.execute(
        select(func.count(OrderExecution.id)).where(
            OrderExecution.dry_run.is_(dry_run_flag),
            OrderExecution.created_at >= today_start,
            OrderExecution.status != "rejected",
        )
    ).scalar_one_or_none() or 0
    if day_order_count >= caps.per_day_max_orders:
        raise _GuardFailure(
            f"per-day order count cap: {day_order_count} >= {caps.per_day_max_orders}"
        )


def _check_cash_sufficiency(adapter: BrokerAdapter, sug: OrderSuggestion) -> None:
    """Raise _GuardFailure if buying power is insufficient."""
    if sug.side != "buy":
        return
    account = adapter.get_account()
    required = sug.qty * sug.limit_price
    if account.buying_power_usd < required:
        raise _GuardFailure(
            f"insufficient buying power: ${account.buying_power_usd:,.0f}"
            f" < ${required:,.0f} required for {sug.ticker}"
        )


def _trigger_kill_switch(
    session: Session,
    emailer: EmailSender,
    adapter: BrokerAdapter,
    trigger: str,
    detail: str,
    settings_email_to: str,
) -> None:
    """Flip mode to OFF, cancel open auto-trade orders, log, and alert."""
    logger.error("AUTO-TRADE KILL SWITCH: trigger=%s detail=%s", trigger, detail)

    # Flip mode to OFF
    meta = session.get(Meta, META_KEY_MODE)
    if meta is not None:
        meta.value = "OFF"
    else:
        session.add(Meta(key=META_KEY_MODE, value="OFF"))

    # Find and cancel recent auto-trade placed orders (last 24h, real, accepted_for_routing)
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    recent_executions = session.scalars(
        select(OrderExecution).where(
            OrderExecution.match_method == "auto_trade_placed",
            OrderExecution.dry_run.is_(False),
            OrderExecution.created_at >= cutoff,
            OrderExecution.status == "accepted_for_routing",
        )
    ).all()
    cancelled_ids: list[str] = []
    for exe in recent_executions:
        if exe.broker_order_id:
            try:
                adapter.cancel_order(exe.broker_order_id)
                cancelled_ids.append(exe.broker_order_id)
            except Exception as cancel_exc:
                logger.error(
                    "kill_switch: cancel_order(%s) failed: %s",
                    exe.broker_order_id,
                    cancel_exc,
                )

    session.add(
        KillSwitchLog(
            ts=datetime.now(UTC),
            trigger=trigger,
            detail=detail,
            cancelled_order_ids=cancelled_ids,
        )
    )
    session.flush()

    # Alert email
    try:
        subject = f"ALERT: Auto-trade kill switch activated ({trigger})"
        body = (
            f"Kill switch triggered: {trigger}\n\n{detail}\n\n"
            f"Cancelled orders: {', '.join(cancelled_ids) or 'none'}\n\n"
            "Auto-trade mode set to OFF. Manual re-promotion required."
        )
        emailer.send(
            to=settings_email_to,
            subject=subject,
            html=f"<pre>{body}</pre>",
            text=body,
        )
    except Exception as email_exc:
        logger.error("kill_switch: alert email failed: %s", email_exc)


def run_auto_trade_pass(
    session: Session,
    adapter: BrokerAdapter,
    emailer: EmailSender,
    email_to: str,
    broker: str,
) -> list[AutoTradeOutcome]:
    """Single auto-trade pass: fetch accepted suggestions, run guards, place/simulate orders.

    Args:
        session: open SQLAlchemy session. Caller manages commit (session_scope).
        adapter: broker adapter (Alpaca or Moomoo).
        emailer: for kill-switch alert emails.
        email_to: alert recipient.
        broker: broker name string stored on OrderExecution rows (e.g. "alpaca", "moomoo").

    Returns list of AutoTradeOutcome for each suggestion processed.
    Kill switch is triggered on readback mismatch or broker error — mode flips to OFF.
    Per-suggestion guard failures (_GuardFailure) skip the suggestion without killing.
    """
    mode = _get_mode(session)
    if mode == "OFF":
        logger.debug("run_auto_trade_pass: mode=OFF, nothing to do")
        return []

    caps = _get_active_caps(session)
    suggestions = _fetch_accepted_unexecuted(session)
    if not suggestions:
        logger.info(
            "run_auto_trade_pass: mode=%s, no accepted unexecuted suggestions", mode
        )
        return []

    logger.info(
        "run_auto_trade_pass: mode=%s, processing %d suggestions",
        mode,
        len(suggestions),
    )

    outcomes: list[AutoTradeOutcome] = []

    for sug in suggestions:
        try:
            _check_idempotency(session, sug, mode)
            _check_wash_sale(session, sug)
            if caps is not None:
                _check_caps(session, sug, caps, mode)
            _check_cash_sufficiency(adapter, sug)
        except _GuardFailure as gf:
            logger.info("auto_trade: sug-%d skipped by guard: %s", sug.id, gf)
            outcomes.append(
                AutoTradeOutcome(
                    suggestion_id=sug.id,
                    placed=False,
                    dry_run=mode == "DRY_RUN",
                    broker_order_id=None,
                    rejected_reason=str(gf),
                )
            )
            continue

        client_order_id = f"sug-{sug.id}"

        if mode == "DRY_RUN":
            session.add(
                OrderExecution(
                    suggestion_id=sug.id,
                    ticker=sug.ticker,
                    side=sug.side,
                    submitted_qty=sug.qty,
                    filled_qty=0,
                    limit_price=sug.limit_price,
                    broker="dry_run",
                    broker_order_id=None,
                    client_order_id=client_order_id,
                    dry_run=True,
                    status="dry_run",
                    match_method="auto_trade_placed",
                    match_confidence=1.0,
                    created_at=datetime.now(UTC),
                )
            )
            session.flush()
            outcomes.append(
                AutoTradeOutcome(
                    suggestion_id=sug.id,
                    placed=True,
                    dry_run=True,
                    broker_order_id=None,
                    rejected_reason=None,
                )
            )
            logger.info(
                "auto_trade: DRY_RUN sug-%d %s %s %.0f@%.2f",
                sug.id,
                sug.ticker,
                sug.side,
                sug.qty,
                sug.limit_price,
            )

        else:  # LIVE
            req = OrderRequest(
                client_order_id=client_order_id,
                ticker=sug.ticker,
                side=sug.side,  # type: ignore[arg-type]
                qty=sug.qty,
                limit_price=sug.limit_price,
                time_in_force="day",
            )
            try:
                conf = adapter.submit_order(req)
            except Exception as broker_exc:
                _trigger_kill_switch(
                    session,
                    emailer,
                    adapter,
                    trigger="broker_error",
                    detail=f"submit_order for sug-{sug.id} raised: {broker_exc}",
                    settings_email_to=email_to,
                )
                outcomes.append(
                    AutoTradeOutcome(
                        suggestion_id=sug.id,
                        placed=False,
                        dry_run=False,
                        broker_order_id=None,
                        rejected_reason=f"broker_error: {broker_exc}",
                    )
                )
                break  # kill switch fired — stop processing

            # Read-back verification
            try:
                readback = adapter.get_order(conf.broker_order_id)
                if readback.client_order_id != client_order_id:
                    _trigger_kill_switch(
                        session,
                        emailer,
                        adapter,
                        trigger="readback_mismatch",
                        detail=(
                            f"sug-{sug.id}: submitted client_order_id={client_order_id!r}"
                            f" but read back {readback.client_order_id!r}"
                        ),
                        settings_email_to=email_to,
                    )
                    outcomes.append(
                        AutoTradeOutcome(
                            suggestion_id=sug.id,
                            placed=False,
                            dry_run=False,
                            broker_order_id=conf.broker_order_id,
                            rejected_reason="readback_mismatch",
                        )
                    )
                    break  # kill switch fired — stop processing
            except Exception as readback_exc:
                _trigger_kill_switch(
                    session,
                    emailer,
                    adapter,
                    trigger="readback_failed",
                    detail=f"get_order({conf.broker_order_id}) raised: {readback_exc}",
                    settings_email_to=email_to,
                )
                outcomes.append(
                    AutoTradeOutcome(
                        suggestion_id=sug.id,
                        placed=False,
                        dry_run=False,
                        broker_order_id=conf.broker_order_id,
                        rejected_reason=f"readback_failed: {readback_exc}",
                    )
                )
                break

            # Success — record the execution
            session.add(
                OrderExecution(
                    suggestion_id=sug.id,
                    ticker=sug.ticker,
                    side=sug.side,
                    submitted_qty=sug.qty,
                    filled_qty=0,
                    limit_price=sug.limit_price,
                    broker=broker,
                    broker_order_id=conf.broker_order_id,
                    client_order_id=client_order_id,
                    dry_run=False,
                    status="accepted_for_routing",
                    match_method="auto_trade_placed",
                    match_confidence=1.0,
                    created_at=datetime.now(UTC),
                )
            )
            session.flush()
            outcomes.append(
                AutoTradeOutcome(
                    suggestion_id=sug.id,
                    placed=True,
                    dry_run=False,
                    broker_order_id=conf.broker_order_id,
                    rejected_reason=None,
                )
            )
            logger.info(
                "auto_trade: LIVE sug-%d %s %s %.0f@%.2f → broker_order_id=%s",
                sug.id,
                sug.ticker,
                sug.side,
                sug.qty,
                sug.limit_price,
                conf.broker_order_id,
            )

    return outcomes
