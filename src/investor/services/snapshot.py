"""Snapshot service: pull positions + account from broker, persist to DB."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..brokers.base import Account, BrokerAdapter
from ..config import Settings
from ..models import BrokerAccount, PositionsSnapshot

logger = logging.getLogger(__name__)


def take_snapshot(
    adapter: BrokerAdapter, session: Session, settings: Settings, broker_account_id: int
) -> int:
    """Pull account + positions for ONE broker account and persist in one transaction.

    Returns the number of position rows written. All rows are tagged with
    ``broker_account_id`` (= the account's stable account_ref).
    BrokerAccount: inserts a new state row only when cash or equity changed; otherwise
    updates last_sync in place to avoid unbounded table growth.
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
                broker_account_id=broker_account_id,
                account_id=account.account_id,
                ts=p.as_of,
                ticker=p.ticker,
                qty=p.qty,
                avg_cost=p.avg_cost,
                market_value=p.market_value,
                weight_pct=weight_pct,
                currency=p.currency,
            )
        )

    # Write zero-qty tombstones for any ticker that appeared recently but is no
    # longer returned by the broker (position fully closed since last sync). Scoped
    # to this account so one broker's closed position doesn't tombstone another's.
    current_tickers = {p.ticker for p in positions}
    cutoff = datetime.now(UTC) - timedelta(days=2)
    recent_rows = session.execute(
        text(
            "SELECT DISTINCT ticker FROM positions_snapshot "
            "WHERE ts >= :cutoff AND broker_account_id = :bid"
        ),
        {"cutoff": cutoff.isoformat(), "bid": broker_account_id},
    ).fetchall()
    for (ticker,) in recent_rows:
        if ticker not in current_tickers:
            rows.append(
                PositionsSnapshot(
                    broker_account_id=broker_account_id,
                    account_id=account.account_id,
                    ts=account.as_of,
                    ticker=ticker,
                    qty=0.0,
                    avg_cost=0.0,
                    market_value=0.0,
                    weight_pct=0.0,
                    currency=account.currency,
                )
            )
            logger.info("Snapshot: tombstone written for closed position %s", ticker)

    session.add_all(rows)
    _write_broker_account(session, account, broker_name, mode, broker_account_id)
    session.flush()

    logger.info(
        "Snapshot written: %d position rows, equity=$%.2f",
        len(rows), account.equity_usd,
    )
    return len(rows)


def _write_broker_account(
    session: Session,
    account: Account,
    broker_name: str,
    mode: str,
    broker_account_id: int,
) -> None:
    """Close-and-insert the cash/equity state row for one account (by account_ref).

    The new state row carries the account's stable identity forward — account_ref and
    the identity columns (nickname/is_active/connection_config), plus broker/mode — so
    a snapshot never silently re-brands an account from the global ``settings.broker``.
    """
    latest = (
        session.query(BrokerAccount)
        .filter(
            BrokerAccount.account_ref == broker_account_id,
            BrokerAccount.effective_to.is_(None),
        )
        .order_by(BrokerAccount.last_sync.desc())
        .first()
    )

    same_values = (
        latest is not None
        and latest.account_id == account.account_id
        and abs(latest.cash_usd - account.cash_usd) <= 0.01
        and abs(latest.equity_usd - account.equity_usd) <= 0.01
    )

    if same_values:
        assert latest is not None  # guaranteed by same_values (which includes latest is not None)
        latest.last_sync = account.as_of
        logger.info("BrokerAccount unchanged — updated last_sync only")
    else:
        if latest is not None:
            latest.effective_to = account.as_of
        session.add(
            BrokerAccount(
                account_ref=broker_account_id,
                account_id=account.account_id,
                broker=latest.broker if latest is not None else broker_name,
                mode=latest.mode if latest is not None else mode,
                nickname=latest.nickname if latest is not None else None,
                is_active=latest.is_active if latest is not None else True,
                connection_config=latest.connection_config if latest is not None else None,
                cash_usd=account.cash_usd,
                equity_usd=account.equity_usd,
                last_sync=account.as_of,
                effective_from=account.as_of,
            )
        )
        logger.info(
            "BrokerAccount changed (cash=%.2f, equity=%.2f) — new row inserted",
            account.cash_usd, account.equity_usd,
        )
