"""Funds-flow detection (P2.3, ADR-0035).

Cash-flow heuristic: an external transfer (deposit/withdrawal) moves cash without a matching
trade. So the day's Δcash that ISN'T explained by that day's buy/sell fills is the external
flow. Sub-threshold flows (dividends, fees, interest) are ignored by the dollar floor; large
ones are surfaced for the user to interpret.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BrokerAccount, OrderExecution

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class FundsFlow:
    """A detected external cash flow for one broker account."""

    broker_account_id: int
    delta_usd: float          # signed external flow (+ deposit, − withdrawal)
    kind: str                 # "deposit" | "withdrawal"
    prev_cash: float
    cur_cash: float
    trade_cash_flow: float    # sells − buys over the window
    note: str | None = None


def _today_start_utc() -> datetime:
    """Midnight ET today, as a UTC datetime (for comparing against UTC last_sync)."""
    now_et = datetime.now(_ET)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_et.astimezone(UTC)


def detect_funds_flow(
    session: Session, broker_account_id: int, *, threshold: float
) -> FundsFlow | None:
    """Detect an external cash flow vs the prior day for one account, or None.

    ``cur`` = latest broker_account state row; ``prev`` = latest row whose sync predates
    today (ET). ``external_flow = (cur.cash − prev.cash) − trade_cash_flow``, where
    ``trade_cash_flow = Σ sell proceeds − Σ buy cost`` over fills in ``(prev.last_sync, now]``.
    Returns None when there's no prior row or the flow is within ``threshold``.
    """
    cur = session.scalars(
        select(BrokerAccount)
        .where(
            BrokerAccount.account_ref == broker_account_id,
            BrokerAccount.effective_to.is_(None),
        )
        .order_by(BrokerAccount.last_sync.desc())
    ).first()
    if cur is None:
        return None

    today_start = _today_start_utc()
    prev = session.scalars(
        select(BrokerAccount)
        .where(
            BrokerAccount.account_ref == broker_account_id,
            BrokerAccount.last_sync < today_start,
        )
        .order_by(BrokerAccount.last_sync.desc())
    ).first()
    if prev is None:
        return None  # account younger than a day — nothing to compare

    fills = session.scalars(
        select(OrderExecution).where(
            OrderExecution.broker_account_id == broker_account_id,
            OrderExecution.dry_run.is_(False),
            OrderExecution.filled_at.is_not(None),
            OrderExecution.filled_at > prev.last_sync,
            OrderExecution.filled_at <= cur.last_sync,
        )
    ).all()
    trade_cash_flow = 0.0
    for f in fills:
        if f.filled_price is None:
            continue
        value = f.filled_qty * f.filled_price
        trade_cash_flow += value if f.side == "sell" else -value

    external_flow = (cur.cash_usd - prev.cash_usd) - trade_cash_flow
    if abs(external_flow) <= threshold:
        return None

    return FundsFlow(
        broker_account_id=broker_account_id,
        delta_usd=external_flow,
        kind="deposit" if external_flow > 0 else "withdrawal",
        prev_cash=prev.cash_usd,
        cur_cash=cur.cash_usd,
        trade_cash_flow=trade_cash_flow,
        note=f"window {prev.last_sync:%Y-%m-%d %H:%M}→{cur.last_sync:%Y-%m-%d %H:%M} UTC",
    )
