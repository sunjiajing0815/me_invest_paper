"""Gap service: compute allocation gap between current holdings and targets."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from ..queries import gap_allocation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GapRow:
    ticker: str
    current_pct: float
    target_pct: float
    gap_pct: float
    gap_usd: float
    band_status: Literal["under", "in_band", "over"]


def compute_gap(session: Session) -> list[GapRow]:
    """Run the gap SQL and return one GapRow per target ticker.

    Returns an empty list if no broker_account or target_allocation rows exist.
    """
    result = session.execute(gap_allocation).fetchall()
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
