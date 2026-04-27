"""Snapshot service: pull positions + account from broker, persist to DB."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..brokers.base import BrokerAdapter
from ..config import Settings
from ..models import BrokerAccount, PositionsSnapshot

logger = logging.getLogger(__name__)


def take_snapshot(adapter: BrokerAdapter, session: Session, settings: Settings) -> int:
    """Pull account + positions from broker and persist in one transaction.

    Returns the number of position rows written.
    Always writes one BrokerAccount row regardless of position count.
    """
    account = adapter.get_account()
    positions = adapter.get_positions()
    total_equity = account.equity_usd

    broker_name, _, mode_hint = settings.broker.partition("_")
    mode = mode_hint if mode_hint else "live"

    rows: list[PositionsSnapshot] = []
    for p in positions:
        weight_pct = (p.market_value / total_equity * 100.0) if total_equity else 0.0
        rows.append(
            PositionsSnapshot(
                ts=p.as_of,
                ticker=p.ticker,
                qty=p.qty,
                avg_cost=p.avg_cost,
                market_value=p.market_value,
                weight_pct=weight_pct,
            )
        )

    session.add_all(rows)
    session.add(
        BrokerAccount(
            broker=broker_name,
            mode=mode,
            cash_usd=account.cash_usd,
            equity_usd=account.equity_usd,
            last_sync=account.as_of,
        )
    )
    session.flush()

    logger.info(
        "Snapshot written: %d position rows, equity=$%.2f",
        len(rows), account.equity_usd,
    )
    return len(rows)
