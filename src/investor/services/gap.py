"""Gap service: compute allocation gap between current holdings and targets."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from ..queries import gap_allocation, untracked_positions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GapRow:
    ticker: str
    current_pct: float
    target_pct: float
    gap_pct: float
    gap_usd: float
    band_status: Literal["under", "in_band", "over"]


@dataclass(frozen=True)
class UntrackedPosition:
    ticker: str
    qty: float
    market_value: float
    weight_pct: float


def get_untracked_positions(
    session: Session, broker_account_id: int
) -> list[UntrackedPosition]:
    """Return positions held in one broker account that have no active target allocation."""
    result = session.execute(
        untracked_positions, {"broker_account_id": broker_account_id}
    ).fetchall()
    return [
        UntrackedPosition(
            ticker=row.ticker,
            qty=float(row.qty),
            market_value=float(row.market_value),
            weight_pct=float(row.weight_pct),
        )
        for row in result
    ]


def compute_gap(session: Session, broker_account_id: int) -> list[GapRow]:
    """Run the gap SQL for one broker account and return one GapRow per target ticker.

    Returns an empty list if the account has no broker_account or target_allocation rows.
    """
    result = session.execute(
        gap_allocation, {"broker_account_id": broker_account_id}
    ).fetchall()
    rows = [
        GapRow(
            ticker=row.ticker,
            current_pct=float(row.current_pct),
            target_pct=float(row.target_pct),
            gap_pct=float(row.gap_pct),
            gap_usd=float(row.gap_usd),
            band_status=row.band_status,
        )
        for row in result
    ]
    logger.info("compute_gap returned %d rows", len(rows))
    return rows
