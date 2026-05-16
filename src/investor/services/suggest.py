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
from .llm_levels import ScoredLevel

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
    confidence_at_creation: float | None = None
    anchor_method: str | None = None


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
# Anchor selection
# ---------------------------------------------------------------------------

def select_anchor(
    levels: list[ScoredLevel],
    current_price: float,
    *,
    max_distance_pct: float = 8.0,
    min_confidence: float = 0.4,
) -> ScoredLevel | None:
    """Return the best S/R anchor for limit-price selection.

    First filters to levels within *max_distance_pct* of *current_price*.
    Among those, prefers the level with the highest confidence score (if any
    meet *min_confidence*).  Falls back to the nearest level by absolute
    distance when none meet the confidence threshold.  Returns None when no
    levels are within the distance band at all.
    """
    in_band = [
        lv for lv in levels
        if abs((lv.price / current_price - 1) * 100) <= max_distance_pct
    ]
    if not in_band:
        return None
    scored = [lv for lv in in_band if lv.confidence >= min_confidence]
    if scored:
        return max(scored, key=lambda lv: lv.confidence)
    return min(in_band, key=lambda lv: abs(lv.price - current_price))  # fallback


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def generate_suggestions(
    *,
    gap_rows: list[GapRow],
    nearby_levels: dict[str, NearbyLevels],
    account: AccountSnapshot,
    sizing_rule: SizingRule = HALF_THE_GAP,
    scored_levels: dict[str, list[ScoredLevel]] | None = None,
    cash_floor: float = 100.0,
    max_distance_pct: float = 8.0,
) -> list[OrderSuggestionRow]:
    """Pure function: return a list of order suggestions.

    Processes gap_rows in order (already sorted by abs gap desc).

    When *scored_levels* is provided and contains a non-empty list for a
    ticker, ``select_anchor`` is used to choose the limit price and
    ``confidence_at_creation`` is populated on the resulting row.  When
    *scored_levels* is absent, empty, or ``select_anchor`` returns None,
    the function falls back to the Phase 2 nearest-distance logic
    (``nearby_levels[ticker].supports[0]`` / ``.resistances[0]``) and
    sets ``confidence_at_creation`` to None.
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
            # --- Scored-levels path ---
            confidence_at_creation: float | None = None
            anchor_price: float | None = None
            anchor_method: str | None = None
            anchor_rationale: str | None = None
            ticker_scored = (scored_levels or {}).get(g.ticker)
            buy_levels = [lv for lv in (ticker_scored or []) if lv.type == "support"]
            if buy_levels:
                anchor = select_anchor(
                    buy_levels,
                    nearby.current_price or 0.0,
                    max_distance_pct=max_distance_pct,
                )
                if anchor is not None:
                    anchor_price = anchor.price
                    anchor_method = anchor.method
                    confidence_at_creation = anchor.confidence
                    anchor_rationale = anchor.rationale or None

            # --- Phase 2 fallback ---
            if anchor_price is None:
                levels = nearby.supports
                if not levels:
                    continue
                level = levels[0]
                anchor_price = level.price
                anchor_method = level.method

            distance_pct = (
                abs(anchor_price / nearby.current_price - 1) * 100
                if nearby.current_price
                else 999
            )
            if distance_pct > max_distance_pct:
                logger.debug(
                    "generate_suggestions: %s support %.2f is %.1f%% away — skipping",
                    g.ticker, anchor_price, distance_pct,
                )
                continue

            dollars = g.gap_usd * sizing_rule.fraction
            if sizing_rule.max_dollars is not None:
                dollars = min(dollars, sizing_rule.max_dollars)

            qty = _round_qty(dollars, anchor_price)
            cost = qty * anchor_price
            if qty < 1 or cost > cash_remaining - cash_floor:
                continue

            cash_remaining -= cost
            gap_closed_pct = dollars / g.gap_usd * 100 if g.gap_usd else 0
            conf_str = f" (conf {confidence_at_creation:.2f})" if confidence_at_creation is not None else ""
            rationale_str = f" {anchor_rationale}" if anchor_rationale else ""
            out.append(OrderSuggestionRow(
                ticker=g.ticker,
                side="buy",
                qty=qty,
                limit_price=round(anchor_price, 2),
                reason=(
                    f"underweight {g.gap_pct:+.1f}% — buy at {anchor_method} "
                    f"${anchor_price:,.2f}{conf_str},{rationale_str} closes ~{gap_closed_pct:.0f}% of gap"
                ),
                expires_at=_next_friday_eod(),
                confidence_at_creation=confidence_at_creation,
                anchor_method=anchor_method,
            ))

        elif g.band_status == "over":  # overweight → trim at nearest resistance
            # --- Scored-levels path ---
            confidence_at_creation_sell: float | None = None
            anchor_price_sell: float | None = None
            anchor_method_sell: str | None = None
            anchor_rationale_sell: str | None = None
            ticker_scored_sell = (scored_levels or {}).get(g.ticker)
            sell_levels = [lv for lv in (ticker_scored_sell or []) if lv.type == "resistance"]
            if sell_levels:
                anchor_sell = select_anchor(
                    sell_levels,
                    nearby.current_price or 0.0,
                    max_distance_pct=max_distance_pct,
                )
                if anchor_sell is not None:
                    anchor_price_sell = anchor_sell.price
                    anchor_method_sell = anchor_sell.method
                    confidence_at_creation_sell = anchor_sell.confidence
                    anchor_rationale_sell = anchor_sell.rationale or None

            # --- Phase 2 fallback ---
            if anchor_price_sell is None:
                levels = nearby.resistances
                if not levels:
                    continue
                level = levels[0]
                anchor_price_sell = level.price
                anchor_method_sell = level.method

            distance_pct = (
                abs(anchor_price_sell / nearby.current_price - 1) * 100
                if nearby.current_price
                else 999
            )
            if distance_pct > max_distance_pct:
                logger.debug(
                    "generate_suggestions: %s resistance %.2f is %.1f%% away — skipping",
                    g.ticker, anchor_price_sell, distance_pct,
                )
                continue

            trim_usd = abs(g.gap_usd) * sizing_rule.fraction
            if sizing_rule.max_dollars is not None:
                trim_usd = min(trim_usd, sizing_rule.max_dollars)

            qty = _round_qty(trim_usd, anchor_price_sell)
            if qty < 1:
                continue

            gap_closed_pct = trim_usd / abs(g.gap_usd) * 100 if g.gap_usd else 0
            conf_str_sell = f" (conf {confidence_at_creation_sell:.2f})" if confidence_at_creation_sell is not None else ""
            rationale_str_sell = f" {anchor_rationale_sell}" if anchor_rationale_sell else ""
            out.append(OrderSuggestionRow(
                ticker=g.ticker,
                side="sell",
                qty=qty,
                limit_price=round(anchor_price_sell, 2),
                reason=(
                    f"overweight {g.gap_pct:+.1f}% — trim at {anchor_method_sell} "
                    f"${anchor_price_sell:,.2f}{conf_str_sell},{rationale_str_sell} closes ~{gap_closed_pct:.0f}% of gap"
                ),
                expires_at=_next_friday_eod(),
                confidence_at_creation=confidence_at_creation_sell,
                anchor_method=anchor_method_sell,
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
) -> list[int]:
    """Upsert suggestions for week_of. Never overwrites accepted/rejected rows.

    Returns a list of suggestion IDs (inserted or pre-existing) for this week,
    which callers can use to generate HMAC tokens or build action links.
    """
    ids: list[int] = []
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
            ids.append(existing.id)
            continue

        new_row = OrderSuggestion(
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
            confidence_at_creation=r.confidence_at_creation,
            anchor_method=r.anchor_method,
        )
        session.add(new_row)
        session.flush()
        ids.append(new_row.id)

    return ids
