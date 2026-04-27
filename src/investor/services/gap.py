"""Gap service: compute allocation gap between current holdings and targets."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_GAP_SQL = text("""
WITH latest AS (
  SELECT ticker, weight_pct, market_value,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
  FROM positions_snapshot
),
current AS (SELECT ticker, weight_pct, market_value FROM latest WHERE rn = 1),
account AS (SELECT equity_usd FROM broker_account ORDER BY last_sync DESC LIMIT 1),
targets AS (
  SELECT ticker, target_pct, band_low_pct, band_high_pct
  FROM target_allocation
  WHERE effective_to IS NULL
)
SELECT
  t.ticker,
  COALESCE(c.weight_pct, 0)                                        AS current_pct,
  t.target_pct,
  t.target_pct - COALESCE(c.weight_pct, 0)                         AS gap_pct,
  (t.target_pct - COALESCE(c.weight_pct, 0)) / 100 * a.equity_usd AS gap_usd
FROM targets t
LEFT JOIN current c USING (ticker)
CROSS JOIN account a
ORDER BY ABS(gap_pct) DESC
""")


@dataclass(frozen=True)
class GapRow:
    ticker: str
    current_pct: float
    target_pct: float
    gap_pct: float
    gap_usd: float


def compute_gap(session: Session) -> list[GapRow]:
    """Run the gap SQL and return one GapRow per target ticker.

    Returns an empty list if no broker_account or target_allocation rows exist.
    """
    result = session.execute(_GAP_SQL).fetchall()
    rows = [
        GapRow(
            ticker=row.ticker,
            current_pct=float(row.current_pct),
            target_pct=float(row.target_pct),
            gap_pct=float(row.gap_pct),
            gap_usd=float(row.gap_usd),
        )
        for row in result
    ]
    logger.info("compute_gap returned %d rows", len(rows))
    return rows
