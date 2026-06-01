"""Reconciliation service: match broker activities to order suggestions.

Runs after market close (or on demand) to link filled broker orders back to
the suggestions that prompted them, and to capture untracked manual trades.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..brokers.base import Activity, BrokerAdapter
from ..models import OrderExecution, OrderSuggestion

logger = logging.getLogger(__name__)

_SUG_ID_RE = re.compile(r"^sug-(?P<id>\d+)(?:-r\d+)?$")


def _parse_suggestion_id(client_order_id: str) -> int | None:
    """Extract suggestion id from 'sug-N' or 'sug-N-rK'. Returns None if malformed."""
    m = _SUG_ID_RE.match(client_order_id)
    return int(m.group("id")) if m else None


_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"canceled", "expired", "rejected", "done_for_day", "replaced"}
)


@dataclass(frozen=True)
class MatchResult:
    suggestion_id: int | None
    activity: Activity
    method: Literal["auto_matched", "manual_review", "untracked"]
    confidence: float  # 0..1


def reconcile_activities(
    *,
    session: Session,
    adapter: BrokerAdapter,
    since: datetime,
    broker_account_id: int,
    until: datetime | None = None,
    ticker_tolerance_window_hours: int = 48,
    price_tolerance_pct: float = 0.5,
) -> list[MatchResult]:
    """Match broker fill activities to pending suggestions for ONE broker account.

    Returns one MatchResult per activity that has a filled_at timestamp.
    Activities with no filled_at are silently skipped (unusual broker edge case).
    """
    activities = adapter.get_activities(since, until)
    pending = session.scalars(
        select(OrderSuggestion).where(
            OrderSuggestion.status == "accepted",
            OrderSuggestion.broker_account_id == broker_account_id,
        )
    ).all()

    results: list[MatchResult] = []

    for act in activities:
        if act.filled_at is None:
            continue

        # Rule 1: client_order_id carries "sug-N" → placed by auto-trade engine
        if act.client_order_id and act.client_order_id.startswith("sug-"):
            sid = _parse_suggestion_id(act.client_order_id)
            if sid is not None:
                results.append(
                    MatchResult(
                        suggestion_id=sid,
                        activity=act,
                        method="auto_matched",
                        confidence=1.0,
                    )
                )
                continue
            # malformed sug-* id — fall through to heuristic

        # Rules 2/3: heuristic match on ticker + side + time window + price proximity
        candidates = [
            s
            for s in pending
            if s.ticker == act.ticker
            and s.side == act.side
            and 0
            <= (act.filled_at - s.created_at).total_seconds()
            < ticker_tolerance_window_hours * 3600
            and abs(act.filled_price - s.limit_price) / s.limit_price
            <= price_tolerance_pct / 100
        ]

        if len(candidates) == 1:
            results.append(
                MatchResult(
                    suggestion_id=candidates[0].id,
                    activity=act,
                    method="auto_matched",
                    confidence=0.9,
                )
            )
        elif len(candidates) > 1:
            best = min(
                candidates,
                key=lambda s: abs(s.qty - act.filled_qty) + abs(s.limit_price - act.filled_price),
            )
            results.append(
                MatchResult(
                    suggestion_id=best.id,
                    activity=act,
                    method="manual_review",
                    confidence=0.5,
                )
            )
        else:
            results.append(
                MatchResult(
                    suggestion_id=None,
                    activity=act,
                    method="untracked",
                    confidence=0.0,
                )
            )

    return results


def persist_reconciliation(
    session: Session,
    results: list[MatchResult],
    broker: str,
    broker_account_id: int,
) -> None:
    """Upsert MatchResults into order_execution and flip matched suggestions to 'filled'.

    All inserted/updated rows are scoped to broker_account_id.
    """
    for r in results:
        existing = session.scalar(
            select(OrderExecution).where(
                # Match by broker_order_id (unique per order) + account, NOT the broker
                # string. The placement row may carry the full broker ("alpaca_paper",
                # written by the back-compat auto-trade entrypoint) while reconciliation
                # passes the bare family ("alpaca"); requiring equality missed the
                # placement row and inserted a duplicate filled row, leaving the original
                # stuck at accepted_for_routing. broker_order_id + account is exact.
                OrderExecution.broker_order_id == r.activity.broker_order_id,
                OrderExecution.broker_account_id == broker_account_id,
                OrderExecution.dry_run.is_(False),  # NEVER match against DRY_RUN rows
            )
        )

        if existing:
            existing.filled_qty = r.activity.filled_qty
            existing.filled_price = r.activity.filled_price
            existing.filled_at = r.activity.filled_at
            existing.status = r.activity.status
            if r.activity.side == "sell":
                existing.realized_pnl_usd = compute_realized_pnl(session, r.activity)
            # match_method stays as "auto_trade_placed" — don't overwrite
        else:
            session.add(
                OrderExecution(
                    broker_account_id=broker_account_id,
                    suggestion_id=r.suggestion_id,
                    ticker=r.activity.ticker,
                    side=r.activity.side,
                    filled_qty=r.activity.filled_qty,
                    filled_price=r.activity.filled_price,
                    filled_at=r.activity.filled_at,
                    broker=broker,
                    broker_order_id=r.activity.broker_order_id,
                    client_order_id=r.activity.client_order_id,
                    dry_run=False,  # reconciliation rows are always real
                    status=r.activity.status,
                    match_method=r.method,
                    match_confidence=r.confidence,
                    realized_pnl_usd=(
                        compute_realized_pnl(session, r.activity)
                        if r.activity.side == "sell"
                        else None
                    ),
                    created_at=datetime.now(UTC),
                )
            )

        if r.suggestion_id and r.method == "auto_matched" and r.activity.status == "filled":
            sug = session.get(OrderSuggestion, r.suggestion_id)
            if sug and sug.status == "accepted":
                sug.status = "filled"
                sug.acted_at = r.activity.filled_at

    # Caller (job layer) is responsible for committing — session_scope() handles it on exit.


def compute_realized_pnl(session: Session, sell: Activity) -> float | None:
    """FIFO cost basis for a sell. Returns None if no buy history found."""
    prior_buys = session.scalars(
        select(OrderExecution)
        .where(
            OrderExecution.ticker == sell.ticker,
            OrderExecution.side == "buy",
            OrderExecution.dry_run.is_(False),
            OrderExecution.filled_qty > 0,
            OrderExecution.filled_at.isnot(None),
            OrderExecution.filled_at <= sell.filled_at,  # FIFO: only lots before this sell
        )
        .order_by(OrderExecution.filled_at.asc())
    ).all()

    if not prior_buys:
        logger.warning("compute_realized_pnl: no buy history for %s", sell.ticker)
        return None

    sell_qty = sell.filled_qty
    total_cost = 0.0
    for buy in prior_buys:
        if sell_qty <= 0:
            break
        applied = min(sell_qty, buy.filled_qty)
        total_cost += applied * (buy.filled_price or 0.0)
        sell_qty -= applied

    if sell_qty > 0:
        logger.warning(
            "compute_realized_pnl: %s sell qty %.4f exceeds buy history, %.4f unmatched",
            sell.ticker,
            sell.filled_qty,
            sell_qty,
        )

    proceeds = sell.filled_qty * sell.filled_price
    return proceeds - total_cost


def sync_open_order_statuses(
    session: Session, adapter: BrokerAdapter, broker_account_id: int
) -> int:
    """Batch-poll broker for one account's open executions; mark broker_cancelled if terminal.

    Fetches all closed orders in one call instead of N per-row get_order() calls.
    Returns the number of executions updated.

    NOTE: "replaced" status means Alpaca created a new order with a different ID.
    The new order ID is not tracked — treat as broker_cancelled for the old row;
    the new order will reconcile independently via get_activities().

    NOTE: A fill missed by reconcile_activities() will leave a row stuck as
    accepted_for_routing here (this function only marks cancellations). If both
    the fill AND this sync are called, the fill wins: reconciliation sets status='filled'
    after this function sets 'broker_cancelled'. That sequencing is correct.
    """
    open_execs = session.scalars(
        select(OrderExecution).where(
            OrderExecution.broker_account_id == broker_account_id,
            OrderExecution.status == "accepted_for_routing",
            OrderExecution.dry_run.is_(False),
            OrderExecution.broker_order_id.is_not(None),
        )
    ).all()

    if not open_execs:
        return 0

    try:
        closed_orders = adapter.list_orders(status="closed")
    except Exception as exc:
        logger.warning("sync_open_order_statuses: list_orders failed: %s — skipping", exc)
        return 0

    closed_by_id = {c.broker_order_id: c for c in closed_orders}

    updated = 0
    for exe in open_execs:
        conf = closed_by_id.get(exe.broker_order_id)  # type: ignore[arg-type]
        if conf is not None and conf.status in _TERMINAL_STATUSES:
            exe.status = "broker_cancelled"
            updated += 1
            logger.info(
                "sync_open_order_statuses: sug-%s execution %d marked broker_cancelled "
                "(broker status=%s)",
                exe.suggestion_id,
                exe.id,
                conf.status,
            )
    return updated
