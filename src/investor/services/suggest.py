"""Order suggestion engine: gap rows + nearby levels → concrete buy/trim suggestions.

Guards baked in:
  - in-band tickers are skipped
  - support too far away (> max_distance_pct) is skipped
  - cash floor is respected (don't drain cash below floor)
  - sub-1-share suggestions are dropped
  - never overwrites accepted/rejected rows when persisting
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import OrderSuggestion
from .daily_report import AccountSnapshot
from .gap import GapRow
from .levels import NearbyLevels

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderSuggestionRow:
    """Plain-data suggestion — safe after session closes."""

    ticker: str
    side: Literal["buy", "sell"]
    qty: float
    limit_price: float
    reason: str
    expires_at: datetime


@dataclass(frozen=True)
class SizingRule:
    """Controls how many dollars to deploy per suggestion."""

    fraction: float = 0.5   # fraction of the gap_usd to fill (0.5 = half the gap)
    max_dollars: float | None = None  # hard cap per suggestion, None = no cap


HALF_THE_GAP = SizingRule(fraction=0.5)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _next_monday(ref: date | None = None) -> date:
    """Return the coming Monday (or today if today is Monday)."""
    d = ref or datetime.now(UTC).date()
    days_ahead = (7 - d.weekday()) % 7
    return d + timedelta(days=days_ahead)


def _next_friday_eod(ref: date | None = None) -> datetime:
    """Return Friday 21:00 UTC of the current week (used as expires_at)."""
    monday = _next_monday(ref)
    friday = monday + timedelta(days=4)
    return datetime(friday.year, friday.month, friday.day, 21, 0, 0, tzinfo=UTC)


def _round_qty(dollars: float, price: float) -> float:
    """Return whole shares (floor). Returns 0 if price is 0."""
    if price <= 0:
        return 0.0
    return float(int(dollars / price))


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def generate_suggestions(
    *,
    gap_rows: list[GapRow],
    nearby_levels: dict[str, NearbyLevels],
    account: AccountSnapshot,
    sizing_rule: SizingRule = HALF_THE_GAP,
    cash_floor: float = 100.0,
    max_distance_pct: float = 8.0,
) -> list[OrderSuggestionRow]:
    """Pure function: return a list of order suggestions.

    Processes gap_rows in order (already sorted by abs gap desc).
    """
    out: list[OrderSuggestionRow] = []
    cash_remaining = account.cash_usd

    for g in gap_rows:
        if g.band_status == "in_band":
            continue

        nearby = nearby_levels.get(g.ticker)
        if nearby is None:
            continue

        if g.gap_pct > 0:  # underweight → buy at nearest support
            levels = nearby.supports
            if not levels:
                continue
            level = levels[0]
            distance_pct = (
                abs(level.price / nearby.current_price - 1) * 100
                if nearby.current_price
                else 999
            )
            if distance_pct > max_distance_pct:
                logger.debug(
                    "generate_suggestions: %s support %.2f is %.1f%% away — skipping",
                    g.ticker, level.price, distance_pct,
                )
                continue

            dollars = g.gap_usd * sizing_rule.fraction
            if sizing_rule.max_dollars is not None:
                dollars = min(dollars, sizing_rule.max_dollars)

            qty = _round_qty(dollars, level.price)
            cost = qty * level.price
            if qty < 1 or cost > cash_remaining - cash_floor:
                continue

            cash_remaining -= cost
            gap_closed_pct = dollars / g.gap_usd * 100 if g.gap_usd else 0
            out.append(OrderSuggestionRow(
                ticker=g.ticker,
                side="buy",
                qty=qty,
                limit_price=round(level.price, 2),
                reason=(
                    f"underweight {g.gap_pct:+.1f}% — buy at {level.method} "
                    f"${level.price:,.2f}, closes ~{gap_closed_pct:.0f}% of gap"
                ),
                expires_at=_next_friday_eod(),
            ))

        elif g.band_status == "over":  # overweight → trim at nearest resistance
            levels = nearby.resistances
            if not levels:
                continue
            level = levels[0]
            distance_pct = (
                abs(level.price / nearby.current_price - 1) * 100
                if nearby.current_price
                else 999
            )
            if distance_pct > max_distance_pct:
                logger.debug(
                    "generate_suggestions: %s resistance %.2f is %.1f%% away — skipping",
                    g.ticker, level.price, distance_pct,
                )
                continue

            trim_usd = abs(g.gap_usd) * sizing_rule.fraction
            if sizing_rule.max_dollars is not None:
                trim_usd = min(trim_usd, sizing_rule.max_dollars)

            qty = _round_qty(trim_usd, level.price)
            if qty < 1:
                continue

            gap_closed_pct = trim_usd / abs(g.gap_usd) * 100 if g.gap_usd else 0
            out.append(OrderSuggestionRow(
                ticker=g.ticker,
                side="sell",
                qty=qty,
                limit_price=round(level.price, 2),
                reason=(
                    f"overweight {g.gap_pct:+.1f}% — trim at {level.method} "
                    f"${level.price:,.2f}, closes ~{gap_closed_pct:.0f}% of gap"
                ),
                expires_at=_next_friday_eod(),
            ))

    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def persist_suggestions(
    session: Session,
    rows: list[OrderSuggestionRow],
    targets_id: int | None,
    week_of: date,
) -> None:
    """Upsert suggestions for week_of. Never overwrites accepted/rejected rows."""
    for r in rows:
        existing = session.scalars(
            select(OrderSuggestion).where(
                OrderSuggestion.week_of == week_of,
                OrderSuggestion.ticker == r.ticker,
                OrderSuggestion.side == r.side,
            )
        ).first()

        if existing is not None:
            if existing.status == "pending":
                existing.qty = r.qty
                existing.limit_price = r.limit_price
                existing.reason = r.reason
            # accepted/rejected rows are never touched
            continue

        session.add(OrderSuggestion(
            week_of=week_of,
            ticker=r.ticker,
            side=r.side,
            qty=r.qty,
            limit_price=r.limit_price,
            reason=r.reason,
            status="pending",
            target_allocation_id=targets_id,
            created_at=datetime.now(UTC),
            expires_at=r.expires_at,
        ))

    session.flush()
