"""Auto-trade engine: place orders on behalf of accepted suggestions.

This is the ONLY module permitted to call adapter.submit_order() outside of
brokers/. See ADR-0014 and the grep CI test in tests/test_no_unauthorized_submit_order.py.

Mode lifecycle: OFF (default) → DRY_RUN (simulate) → LIVE (real orders).
Mode is stored per-broker in the auto_trade_state table (keyed by account_ref).
Promotion requires soak-window enforcement (see POST /admin/auto-trade/promote).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..brokers.base import BrokerAdapter, BrokerValidationError, OrderRequest
from ..models import (
    AutoTradeCaps,
    AutoTradeState,
    KillSwitchLog,
    OrderExecution,
    OrderSuggestion,
)
from ..services.accounts import (
    resolve_active_account_refs,
    resolve_primary_account_ref,
)
from ..services.email import EmailSender

logger = logging.getLogger(__name__)

__all__ = [
    "AutoTradeOutcome",
    "resolve_active_account_refs",
    "resolve_primary_account_ref",
    "run_auto_trade_pass",
    "set_mode",
]

Mode = Literal["OFF", "DRY_RUN", "LIVE"]


@dataclass(frozen=True)
class AutoTradeOutcome:
    suggestion_id: int
    placed: bool
    dry_run: bool
    broker_order_id: str | None
    rejected_reason: str | None


# Alpaca order statuses considered "live" (not yet settled).
_LIVE_ORDER_STATUSES: frozenset[str] = frozenset(
    {"new", "partially_filled", "pending_new", "accepted", "held", "pending_cancel"}
)


class _GuardFailure(Exception):  # noqa: N818
    """Per-suggestion guard rejection. Does NOT trigger kill switch — just skips this suggestion."""


def _next_client_order_id(session: Session, sug: OrderSuggestion) -> str:
    """Return the client_order_id to use for the next broker submission.

    First attempt uses "sug-{id}". After each cancel Alpaca permanently blocks that
    client_order_id, so subsequent retries use "sug-{id}-r{n}" where n is the number
    of prior broker_cancelled real execution rows for this suggestion.
    """
    cancelled_count: int = session.execute(
        select(func.count(OrderExecution.id)).where(
            OrderExecution.suggestion_id == sug.id,
            OrderExecution.dry_run.is_(False),
            OrderExecution.status == "broker_cancelled",
        )
    ).scalar_one()
    return f"sug-{sug.id}" if cancelled_count == 0 else f"sug-{sug.id}-r{cancelled_count}"


def _get_mode(session: Session, broker_account_id: int) -> Mode:
    """Read the auto-trade mode for one broker account from auto_trade_state.

    Falls back to OFF if no row exists (the safe default, e.g. a newly-onboarded broker).
    """
    state = session.get(AutoTradeState, broker_account_id)
    if state is None:
        return "OFF"
    val = state.mode.upper()
    if val in ("OFF", "DRY_RUN", "LIVE"):
        return val  # type: ignore[return-value]
    return "OFF"


def set_mode(session: Session, broker_account_id: int, mode: Mode) -> None:
    """Upsert the auto-trade mode for one broker account in auto_trade_state."""
    state = session.get(AutoTradeState, broker_account_id)
    if state is None:
        session.add(AutoTradeState(broker_account_id=broker_account_id, mode=mode))
    else:
        state.mode = mode


def _get_active_caps(session: Session) -> AutoTradeCaps | None:
    return session.scalar(
        select(AutoTradeCaps)
        .where(AutoTradeCaps.effective_to.is_(None))
        .order_by(AutoTradeCaps.effective_from.desc())
    )


def _fetch_accepted_unexecuted(
    session: Session, broker_account_id: int, as_of: date | None = None
) -> list[OrderSuggestion]:
    """Return suggestions with status='accepted' that have no matching real execution row.

    Scoped to one broker account. Only looks at the current calendar week (Mon–Sun).
    Suggestions from prior weeks that are still accepted are stale — they should have
    been expired by the expiry sweep and must not be re-traded in a new week.

    Two queries (not N+1): one for accepted suggestions, one for already-executed IDs.
    A DRY_RUN execution row does not exclude the suggestion — only dry_run=False rows count.
    """
    ref_date = as_of if as_of is not None else datetime.now(UTC).date()
    current_week_monday = ref_date - timedelta(days=ref_date.weekday())

    accepted = session.scalars(
        select(OrderSuggestion).where(
            OrderSuggestion.status == "accepted",
            OrderSuggestion.week_of == current_week_monday,
            OrderSuggestion.broker_account_id == broker_account_id,
        )
    ).all()
    if not accepted:
        return []

    sug_ids = [sug.id for sug in accepted]
    already_executed: set[int] = set(
        session.scalars(
            select(OrderExecution.suggestion_id).where(
                OrderExecution.suggestion_id.in_(sug_ids),
                OrderExecution.dry_run.is_(False),
                OrderExecution.status != "broker_cancelled",
            )
        ).all()
    )
    return [sug for sug in accepted if sug.id not in already_executed]


def _check_idempotency(
    session: Session, sug: OrderSuggestion, mode: Mode, broker_account_id: int
) -> None:
    """Raise _GuardFailure if a prior execution already exists for this suggestion."""
    dry_run_flag = mode == "DRY_RUN"
    existing = session.scalar(
        select(OrderExecution).where(
            OrderExecution.suggestion_id == sug.id,
            OrderExecution.broker_account_id == broker_account_id,
            OrderExecution.dry_run.is_(dry_run_flag),
            OrderExecution.status != "broker_cancelled",
        )
    )
    if existing is not None:
        raise _GuardFailure(
            f"sug-{sug.id} already has an execution row (idempotency guard)"
        )


def _check_stale_live_order(
    session: Session, sug: OrderSuggestion, broker_account_id: int
) -> None:
    """Raise _GuardFailure if a different accepted_for_routing execution exists for this
    ticker on the SAME broker account.

    Prevents placing a second live order while a prior-week GTC is still open at the broker.
    Scoped per broker — two live orders for the same ticker across *different* brokers is
    fine (the multi-broker model); two on the *same* broker is what this prevents.
    """
    stale = session.scalars(
        select(OrderExecution).where(
            OrderExecution.ticker == sug.ticker,
            OrderExecution.broker_account_id == broker_account_id,
            OrderExecution.status == "accepted_for_routing",
            OrderExecution.dry_run.is_(False),
            OrderExecution.suggestion_id != sug.id,
        )
    ).first()
    if stale is not None:
        raise _GuardFailure(
            f"stale live order exists for {sug.ticker} (exec_id={stale.id}, "
            f"sug_id={stale.suggestion_id}) — expiry sweep may have missed; skipping"
        )


def _check_wash_sale(session: Session, sug: OrderSuggestion, broker_account_id: int) -> None:
    """Raise _GuardFailure if a real loss-sell for this ticker occurred within 30 calendar days.

    dry_run=False filter is CRITICAL — simulated losses must never block real trades.
    Scoped per broker account in 4.9a (cross-broker / tax-lot wash-sale is Phase 6).
    """
    if sug.side != "buy":
        return
    cutoff = datetime.now(UTC) - timedelta(days=30)
    loss_sell = session.scalar(
        select(OrderExecution).where(
            OrderExecution.ticker == sug.ticker,
            OrderExecution.broker_account_id == broker_account_id,
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
    broker_account_id: int,
) -> None:
    """Raise _GuardFailure if any spending cap would be breached.

    Spend/count sums are scoped per broker account — each broker has its own caps.
    """
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
            OrderExecution.broker_account_id == broker_account_id,
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
            OrderExecution.broker_account_id == broker_account_id,
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
            OrderExecution.broker_account_id == broker_account_id,
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
    broker_account_id: int,
) -> None:
    """Flip this broker's mode to OFF, cancel its open auto-trade orders, log, and alert."""
    logger.error(
        "AUTO-TRADE KILL SWITCH: broker_account_id=%s trigger=%s detail=%s",
        broker_account_id, trigger, detail,
    )

    # Flip this broker's mode to OFF (per-broker — other brokers are unaffected).
    set_mode(session, broker_account_id, "OFF")
    state = session.get(AutoTradeState, broker_account_id)
    if state is not None:
        state.last_kill_switch_event = datetime.now(UTC)

    # Find and cancel recent auto-trade placed orders for THIS broker
    # (last 24h, real, accepted_for_routing).
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    recent_executions = session.scalars(
        select(OrderExecution).where(
            OrderExecution.broker_account_id == broker_account_id,
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
    *,
    broker_account_id: int | None = None,
    as_of: date | None = None,
) -> list[AutoTradeOutcome]:
    """Single auto-trade pass for ONE broker account: fetch accepted suggestions,
    run guards, place/simulate orders.

    Args:
        session: open SQLAlchemy session. Caller manages commit (session_scope).
        adapter: broker adapter (Alpaca or Moomoo) for this account.
        emailer: for kill-switch alert emails.
        email_to: alert recipient.
        broker: broker name string stored on OrderExecution rows (e.g. "alpaca", "moomoo").
        broker_account_id: the account_ref to trade for. Defaults to the primary active
            account when None (single-broker / back-compat).

    Returns list of AutoTradeOutcome for each suggestion processed.
    Kill switch is triggered on readback mismatch or broker error — this broker's mode
    flips to OFF. Per-suggestion guard failures (_GuardFailure) skip without killing.
    """
    if broker_account_id is None:
        broker_account_id = resolve_primary_account_ref(session)
        if broker_account_id is None:
            logger.warning("run_auto_trade_pass: no active broker account — nothing to do")
            return []

    mode = _get_mode(session, broker_account_id)
    if mode == "OFF":
        logger.debug(
            "run_auto_trade_pass: broker_account_id=%s mode=OFF, nothing to do",
            broker_account_id,
        )
        return []

    caps = _get_active_caps(session)
    suggestions = _fetch_accepted_unexecuted(session, broker_account_id, as_of=as_of)
    if not suggestions:
        logger.info(
            "run_auto_trade_pass: broker_account_id=%s mode=%s, no accepted unexecuted"
            " suggestions", broker_account_id, mode,
        )
        return []

    logger.info(
        "run_auto_trade_pass: broker_account_id=%s mode=%s, processing %d suggestions",
        broker_account_id,
        mode,
        len(suggestions),
    )

    outcomes: list[AutoTradeOutcome] = []

    for sug in suggestions:
        try:
            _check_idempotency(session, sug, mode, broker_account_id)
            _check_stale_live_order(session, sug, broker_account_id)
            _check_wash_sale(session, sug, broker_account_id)
            if caps is None:
                logger.warning(
                    "auto_trade: no active AutoTradeCaps row — skipping sug-%d (caps unconfigured)",
                    sug.id,
                )
                raise _GuardFailure("no active AutoTradeCaps row — configure caps first")
            _check_caps(session, sug, caps, mode, broker_account_id)
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

        if mode == "DRY_RUN":
            client_order_id = f"sug-{sug.id}"
            session.add(
                OrderExecution(
                    broker_account_id=broker_account_id,
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
            # If a prior execution was broker_cancelled, check whether the original
            # GTC order is still live at Alpaca (cancel-propagation lag). If yes,
            # re-adopt it rather than placing a duplicate.
            stale = session.scalar(
                select(OrderExecution)
                .where(
                    OrderExecution.suggestion_id == sug.id,
                    OrderExecution.dry_run.is_(False),
                    OrderExecution.status == "broker_cancelled",
                    OrderExecution.broker_order_id.is_not(None),
                )
                .order_by(OrderExecution.created_at.desc())
            )
            if stale is not None:
                try:
                    prev = adapter.get_order(stale.broker_order_id)  # type: ignore[arg-type]
                    if prev.status in _LIVE_ORDER_STATUSES:
                        stale.status = "accepted_for_routing"
                        stale.created_at = datetime.now(UTC)
                        session.flush()
                        outcomes.append(AutoTradeOutcome(
                            suggestion_id=sug.id,
                            placed=True,
                            dry_run=False,
                            broker_order_id=stale.broker_order_id,
                            rejected_reason=None,
                        ))
                        logger.info(
                            "auto_trade: sug-%d GTC re-adopted stale order %s (status=%s)",
                            sug.id, stale.broker_order_id, prev.status,
                        )
                        continue
                except Exception as check_exc:
                    logger.warning(
                        "auto_trade: sug-%d stale-order check(%s) failed: %s — placing fresh order",
                        sug.id, stale.broker_order_id, check_exc,
                    )

            client_order_id = _next_client_order_id(session, sug)
            req = OrderRequest(
                client_order_id=client_order_id,
                ticker=sug.ticker,
                side=sug.side,  # type: ignore[arg-type]
                qty=sug.qty,
                limit_price=sug.limit_price,
                time_in_force="gtc",
            )
            try:
                conf = adapter.submit_order(req)
            except BrokerValidationError as val_exc:
                logger.warning(
                    "sug-%d: broker rejected order (validation), skipping: %s",
                    sug.id, val_exc,
                )
                outcomes.append(
                    AutoTradeOutcome(
                        suggestion_id=sug.id,
                        placed=False,
                        dry_run=False,
                        broker_order_id=None,
                        rejected_reason=f"broker_validation: {val_exc}",
                    )
                )
                # Note: suggestion stays 'accepted' — it will retry next morning.
                # For permanent rejections (e.g. always sub-penny), manually
                # reject the suggestion via the API to stop the retry loop.
                continue   # keep LIVE, try next suggestion
            except Exception as broker_exc:
                _trigger_kill_switch(
                    session,
                    emailer,
                    adapter,
                    trigger="broker_error",
                    detail=f"submit_order for sug-{sug.id} raised: {broker_exc}",
                    settings_email_to=email_to,
                    broker_account_id=broker_account_id,
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
                        broker_account_id=broker_account_id,
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
                    broker_account_id=broker_account_id,
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

            session.add(
                OrderExecution(
                    broker_account_id=broker_account_id,
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
