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
import math
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
    base_qty: float | None = None
    size_factor: float = 1.0
    context_note: str | None = None
    # "regular" | "topup" (plans/topup_suggestions_design.md)
    kind: str = "regular"
    is_highlighted: bool = False


@dataclass(frozen=True)
class SkippedRow:
    """Out-of-band ticker considered but not suggested, with explanation."""

    ticker: str
    gap_pct: float
    side: Literal["buy", "sell"]
    reason: str


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
    """Return the week_of Monday for suggestion generation.

    Mon–Wed: this week's Monday (manual mid-week re-runs stay on the current week).
    Thu–Sun: the upcoming Monday (Thursday is the cutover for the next week's suggestions).
    """
    d = ref or datetime.now(UTC).date()
    weekday = d.weekday()  # 0=Mon … 6=Sun
    if weekday <= 2:  # Mon, Tue, Wed
        return d - timedelta(days=weekday)
    return d + timedelta(days=(7 - weekday) % 7)


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


def _floor2dp(price: float) -> float:
    """Floor to 2 decimal places — use for buy limit prices (never overpay)."""
    return math.floor(price * 100) / 100


def _ceil2dp(price: float) -> float:
    """Ceiling to 2 decimal places — use for sell limit prices (never undersell)."""
    return math.ceil(price * 100) / 100


# ---------------------------------------------------------------------------
# Anchor selection
# ---------------------------------------------------------------------------

def select_anchor(
    levels: list[ScoredLevel],
    current_price: float,
    *,
    max_distance_pct: float = 15.0,
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

@dataclass(frozen=True)
class _BuyAnchor:
    price: float
    method: str
    confidence: float | None
    rationale: str | None


def _select_buy_anchor(
    nearby: NearbyLevels,
    ticker_scored: list[ScoredLevel] | None,
    max_distance_pct: float,
) -> tuple[_BuyAnchor | None, str | None]:
    """Shared buy-anchor selection: scored ``select_anchor`` (supports at/below current
    only) with nearest-support fallback, then the distance guard. Returns
    ``(anchor, None)`` or ``(None, skip_reason)`` — reason strings match the historical
    SkippedRow wording. Used by regular suggestions AND top-ups so both share one
    anchor-quality bar."""
    confidence: float | None = None
    rationale: str | None = None
    price: float | None = None
    method: str | None = None
    cur = nearby.current_price or 0.0
    # Directional: a BUY anchors to a support AT OR BELOW the current price. A scored
    # "support" can sit above current once price drops through it, and anchoring there
    # puts the limit above market (fills immediately, not the intended pullback).
    buy_levels = [
        lv for lv in (ticker_scored or []) if lv.type == "support" and lv.price <= cur
    ]
    if buy_levels:
        anchor = select_anchor(buy_levels, cur, max_distance_pct=max_distance_pct)
        if anchor is not None:
            price = anchor.price
            method = anchor.method
            confidence = anchor.confidence
            rationale = anchor.rationale or None
    if price is None:
        if not nearby.supports:
            return None, "no support levels found"
        level = nearby.supports[0]
        price = level.price
        method = level.method
    distance_pct = (
        abs(price / nearby.current_price - 1) * 100 if nearby.current_price else 999
    )
    if distance_pct > max_distance_pct:
        return None, (
            f"nearest support ({method} ${price:,.2f})"
            f" is {distance_pct:.1f}% away"
            f" — exceeds {max_distance_pct:.0f}% limit"
        )
    assert method is not None
    return _BuyAnchor(price, method, confidence, rationale), None


def generate_suggestions(
    *,
    gap_rows: list[GapRow],
    nearby_levels: dict[str, NearbyLevels],
    account: AccountSnapshot,
    sizing_rule: SizingRule = HALF_THE_GAP,
    scored_levels: dict[str, list[ScoredLevel]] | None = None,
    cash_floor: float = 100.0,
    max_distance_pct: float = 15.0,
) -> tuple[list[OrderSuggestionRow], list[SkippedRow]]:
    """Pure function: return (suggestions, skipped) for out-of-band tickers.

    Processes gap_rows in order (already sorted by abs gap desc).

    When *scored_levels* is provided and contains a non-empty list for a
    ticker, ``select_anchor`` is used to choose the limit price and
    ``confidence_at_creation`` is populated on the resulting row.  When
    *scored_levels* is absent, empty, or ``select_anchor`` returns None,
    the function falls back to the Phase 2 nearest-distance logic
    (``nearby_levels[ticker].supports[0]`` / ``.resistances[0]``) and
    sets ``confidence_at_creation`` to None.

    *skipped* contains one entry per out-of-band ticker that was considered
    but not suggested, with a human-readable reason (distance guard, no levels,
    sub-share qty, cash floor). In-band tickers are never reported.
    """
    out: list[OrderSuggestionRow] = []
    skipped: list[SkippedRow] = []
    cash_remaining = account.cash_usd

    for g in gap_rows:
        if g.band_status == "in_band":
            continue

        nearby = nearby_levels.get(g.ticker)
        if nearby is None:
            skipped.append(SkippedRow(
                ticker=g.ticker,
                gap_pct=g.gap_pct,
                side="buy" if g.gap_pct > 0 else "sell",
                reason="no nearby levels computed",
            ))
            continue

        if g.gap_pct > 0:  # underweight → buy at nearest support
            buy_anchor, skip_reason = _select_buy_anchor(
                nearby, (scored_levels or {}).get(g.ticker), max_distance_pct
            )
            if buy_anchor is None:
                skipped.append(SkippedRow(
                    ticker=g.ticker, gap_pct=g.gap_pct, side="buy",
                    reason=skip_reason or "no buy anchor",
                ))
                continue
            anchor_price = buy_anchor.price
            anchor_method = buy_anchor.method
            confidence_at_creation = buy_anchor.confidence
            anchor_rationale = buy_anchor.rationale

            dollars = g.gap_usd * sizing_rule.fraction
            if sizing_rule.max_dollars is not None:
                dollars = min(dollars, sizing_rule.max_dollars)

            qty = _round_qty(dollars, anchor_price)
            cost = qty * anchor_price
            if qty < 1:
                skipped.append(SkippedRow(
                    ticker=g.ticker, gap_pct=g.gap_pct, side="buy",
                    reason=f"gap too small for a full-share order at ${anchor_price:,.2f}",
                ))
                continue
            if cost > cash_remaining - cash_floor:
                skipped.append(SkippedRow(
                    ticker=g.ticker, gap_pct=g.gap_pct, side="buy",
                    reason=(
                        f"would breach ${cash_floor:.0f} cash floor"
                        " after higher-priority suggestions"
                    ),
                ))
                continue

            cash_remaining -= cost
            gap_closed_pct = dollars / g.gap_usd * 100 if g.gap_usd else 0
            conf_str = (
                f" (conf {confidence_at_creation:.2f})"
                if confidence_at_creation is not None
                else ""
            )
            rationale_str = f" {anchor_rationale}" if anchor_rationale else ""
            out.append(OrderSuggestionRow(
                ticker=g.ticker,
                side="buy",
                qty=qty,
                limit_price=round(anchor_price, 2),
                reason=(
                    f"underweight {g.gap_pct:+.1f}% — buy at {anchor_method} "
                    f"${anchor_price:,.2f}{conf_str},{rationale_str}"
                    f" closes ~{gap_closed_pct:.0f}% of gap"
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
            cur_price_sell = nearby.current_price or 0.0
            # Directional mirror: a SELL/trim anchors to a resistance AT OR ABOVE current.
            sell_levels = [
                lv for lv in (ticker_scored_sell or [])
                if lv.type == "resistance" and lv.price >= cur_price_sell
            ]
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
                    skipped.append(SkippedRow(
                        ticker=g.ticker, gap_pct=g.gap_pct, side="sell",
                        reason="no resistance levels found",
                    ))
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
                skipped.append(SkippedRow(
                    ticker=g.ticker, gap_pct=g.gap_pct, side="sell",
                    reason=(
                        f"nearest resistance ({anchor_method_sell}"
                        f" ${anchor_price_sell:,.2f})"
                        f" is {distance_pct:.1f}% away"
                        f" — exceeds {max_distance_pct:.0f}% limit"
                    ),
                ))
                continue

            trim_usd = abs(g.gap_usd) * sizing_rule.fraction
            if sizing_rule.max_dollars is not None:
                trim_usd = min(trim_usd, sizing_rule.max_dollars)

            qty = _round_qty(trim_usd, anchor_price_sell)
            if qty < 1:
                skipped.append(SkippedRow(
                    ticker=g.ticker, gap_pct=g.gap_pct, side="sell",
                    reason=(
                        f"gap too small for a full-share trim"
                        f" at ${anchor_price_sell:,.2f}"
                    ),
                ))
                continue

            gap_closed_pct = trim_usd / abs(g.gap_usd) * 100 if g.gap_usd else 0
            conf_str_sell = (
                f" (conf {confidence_at_creation_sell:.2f})"
                if confidence_at_creation_sell is not None
                else ""
            )
            rationale_str_sell = f" {anchor_rationale_sell}" if anchor_rationale_sell else ""
            out.append(OrderSuggestionRow(
                ticker=g.ticker,
                side="sell",
                qty=qty,
                limit_price=round(anchor_price_sell, 2),
                reason=(
                    f"overweight {g.gap_pct:+.1f}% — trim at {anchor_method_sell} "
                    f"${anchor_price_sell:,.2f}{conf_str_sell},{rationale_str_sell}"
                    f" closes ~{gap_closed_pct:.0f}% of gap"
                ),
                expires_at=_next_friday_eod(),
                confidence_at_creation=confidence_at_creation_sell,
                anchor_method=anchor_method_sell,
            ))

    return out, skipped


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def persist_suggestions(
    session: Session,
    rows: list[OrderSuggestionRow],
    targets_id: int | None,
    week_of: date,
    broker_account_id: int,
) -> list[int]:
    """Upsert suggestions for (broker_account_id, week_of). Never overwrites
    accepted/rejected rows.

    Returns a list of suggestion IDs (inserted or pre-existing) for this account/week,
    which callers can use to generate HMAC tokens or build action links.
    """
    ids: list[int] = []
    for r in rows:
        existing = session.scalars(
            select(OrderSuggestion).where(
                OrderSuggestion.broker_account_id == broker_account_id,
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
                existing.anchor_method = r.anchor_method
                existing.confidence_at_creation = r.confidence_at_creation
                existing.base_qty = r.base_qty
                existing.size_factor = r.size_factor
                existing.context_note = r.context_note
                existing.kind = r.kind
                existing.is_highlighted = r.is_highlighted
            # accepted/rejected rows are never touched
            ids.append(existing.id)
            continue

        new_row = OrderSuggestion(
            broker_account_id=broker_account_id,
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
            base_qty=r.base_qty,
            size_factor=r.size_factor,
            context_note=r.context_note,
            kind=r.kind,
            is_highlighted=r.is_highlighted,
        )
        session.add(new_row)
        session.flush()
        ids.append(new_row.id)

    return ids


# ---------------------------------------------------------------------------
# Top-up suggestions (plans/topup_suggestions_design.md)
# ---------------------------------------------------------------------------


def topup_size_fraction(vix: float | None, fear_greed: int | None) -> float:
    """Deterministic sentiment fraction for top-up sizing (contrarian: buy more in fear).

    Fear & Greed is primary; VIX is the fallback when F&G is missing; 0.5 (neutral)
    when both are missing. Pure Python over AI-supplied metrics — no LLM decision.
    """
    if fear_greed is not None:
        if fear_greed <= 25:
            return 1.0
        if fear_greed <= 45:
            return 0.75
        if fear_greed <= 55:
            return 0.5
        return 0.25
    if vix is not None:
        if vix >= 30:
            return 1.0
        if vix >= 20:
            return 0.75
        return 0.5
    return 0.5


def generate_topup_suggestions(
    *,
    gap_rows: list[GapRow],
    nearby_levels: dict[str, NearbyLevels],
    account: AccountSnapshot,
    band_high_by_ticker: dict[str, float],
    regular_buy_tickers: set[str],
    sentiment_fraction: float,
    sentiment_note: str,
    cash_available: float,
    scored_levels: dict[str, list[ScoredLevel]] | None = None,
    cash_floor: float = 100.0,
    max_distance_pct: float = 15.0,
) -> list[OrderSuggestionRow]:
    """Pure function: sentiment-sized near-target buys ("top-ups").

    Eligibility (all must hold — plans/topup_suggestions_design.md):
      1. current < target (``gap_pct > 0``) — covers in-band-under AND sub-share-gap cases
      2. no regular buy draft for the ticker this run (mutually exclusive)
      3. a buy anchor via the SAME selection as regular buys (scored + fallback + 15% guard)
      4. >= 1 whole share at the anchor keeps the holding <= band_high
      5. cost fits ``cash_available`` (post-regular-drafts budget) minus the cash floor
         (qty reduced to fit; skipped if not even 1 share fits)

    Sizing: base = max whole shares under band_high; qty = max(1, floor(base × fraction)).
    """
    out: list[OrderSuggestionRow] = []
    equity = account.equity_usd
    if equity <= 0:
        return out
    cash_remaining = cash_available

    for g in gap_rows:
        if g.gap_pct <= 0:
            continue
        if g.ticker in regular_buy_tickers:
            continue
        band_high = band_high_by_ticker.get(g.ticker)
        if band_high is None:
            continue
        nearby = nearby_levels.get(g.ticker)
        if nearby is None:
            continue

        anchor, _reason = _select_buy_anchor(
            nearby, (scored_levels or {}).get(g.ticker), max_distance_pct
        )
        if anchor is None:
            continue

        # base = max whole shares that keep the holding <= band_high at the anchor price
        headroom_pct = band_high - g.current_pct
        base_shares = int(headroom_pct / 100.0 * equity / anchor.price)
        if base_shares < 1:
            continue  # even one share would breach the upper band

        qty = max(1, int(base_shares * sentiment_fraction))
        affordable = int((cash_remaining - cash_floor) / anchor.price)
        qty = min(qty, affordable)
        if qty < 1:
            continue

        cash_remaining -= qty * anchor.price
        conf_str = (
            f" (conf {anchor.confidence:.2f})" if anchor.confidence is not None else ""
        )
        out.append(OrderSuggestionRow(
            ticker=g.ticker,
            side="buy",
            qty=float(qty),
            limit_price=round(anchor.price, 2),
            reason=(
                f"top-up: {g.current_pct:.1f}% vs {g.target_pct:.1f}% target — buy at "
                f"{anchor.method} ${anchor.price:,.2f}{conf_str};"
                f" keeps holding ≤ band {band_high:.0f}%"
            ),
            expires_at=_next_friday_eod(),
            confidence_at_creation=anchor.confidence,
            anchor_method=anchor.method,
            base_qty=float(base_shares),
            size_factor=sentiment_fraction,
            context_note=f"top-up sized ×{sentiment_fraction:g} ({sentiment_note})",
            kind="topup",
        ))
    return out
