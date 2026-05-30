"""Daily report composer: reads DB, returns an immutable DailyReport dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import BrokerAccount
from ..queries import positions_latest
from .gap import GapRow, UntrackedPosition, compute_gap, get_untracked_positions
from .indicators import IndicatorRow
from .levels import NearbyLevels


@dataclass(frozen=True)
class AccountSnapshot:
    """Plain-data copy of a BrokerAccount row — safe to use after the session closes."""

    broker: str
    mode: str
    cash_usd: float
    equity_usd: float


@dataclass(frozen=True)
class DailyReport:
    date: date
    account: AccountSnapshot | None
    positions: list[Any]  # raw SQL named-tuple rows (not ORM objects); session-safe
    gap_rows: list[GapRow]
    drift_alerts: list[GapRow]             # gap_rows where band_status != "in_band"
    indicators: list[IndicatorRow] = field(default_factory=list)
    nearby_levels: dict[str, NearbyLevels] = field(default_factory=dict)
    untracked_positions: list[UntrackedPosition] = field(default_factory=list)


def compose_daily_report(
    session: Session,
    *,
    broker_account_id: int,
    watchlist: list[str] | None = None,
    bars_dir: str = "data/bars",
) -> DailyReport:
    """Pure function for ONE broker account — reads DB (and Parquet if watchlist given),
    returns DailyReport. No I/O. All reads are scoped by broker_account_id."""
    today = datetime.now(UTC).date()

    orm_account = (
        session.query(BrokerAccount)
        .filter(
            BrokerAccount.account_ref == broker_account_id,
            BrokerAccount.effective_to.is_(None),
        )
        .order_by(BrokerAccount.last_sync.desc())
        .first()
    )
    # Convert to a plain dataclass while the session is still open so we don't
    # carry a detached ORM object out of the session scope.
    account: AccountSnapshot | None = (
        AccountSnapshot(
            broker=orm_account.broker,
            mode=orm_account.mode,
            cash_usd=orm_account.cash_usd,
            equity_usd=orm_account.equity_usd,
        )
        if orm_account is not None
        else None
    )

    positions = session.execute(
        positions_latest, {"broker_account_id": broker_account_id}
    ).fetchall()
    gap_rows = compute_gap(session, broker_account_id)
    drift_alerts = [r for r in gap_rows if r.band_status != "in_band"]
    untracked = get_untracked_positions(session, broker_account_id)

    indicators: list[IndicatorRow] = []
    nearby_levels: dict[str, NearbyLevels] = {}

    if watchlist:
        try:
            from .indicators import compute_indicators
            from .levels import build_nearby_levels, compute_levels

            indicators = compute_indicators(watchlist, bars_dir)
            sr_rows = compute_levels(watchlist, indicators, bars_dir)
            nearby_levels = build_nearby_levels(watchlist, sr_rows, indicators)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "compose_daily_report: indicator/level computation failed: %s", exc
            )

    return DailyReport(
        date=today,
        account=account,
        positions=list(positions),
        gap_rows=gap_rows,
        drift_alerts=drift_alerts,
        indicators=indicators,
        nearby_levels=nearby_levels,
        untracked_positions=untracked,
    )
