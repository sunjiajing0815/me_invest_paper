"""Weekly order activity metrics for the Friday review email."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from .. import queries as q

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderFunnel:
    week_of: date
    suggested: int
    accepted: int
    routed_live: int
    filled_live: int
    partial_live: int
    accepted_not_routed: int
    rejected: int
    expired: int
    dry_run_count: int


@dataclass(frozen=True)
class OrderFlow:
    week_of: date
    buy_routed_usd: float
    sell_routed_usd: float
    buy_filled_usd: float
    sell_filled_usd: float
    dry_run_buy_usd: float
    dry_run_sell_usd: float
    distinct_tickers: int


@dataclass(frozen=True)
class AllocationDriftRow:
    ticker: str
    target_pct: float
    current_pct_mon: float
    current_pct_fri: float
    gap_pct_mon: float
    gap_pct_fri: float
    drift_pp: float
    moved_toward_target: bool
    targets_changed_midweek: bool
    monday_is_fallback: bool


@dataclass(frozen=True)
class PerTickerWeekRow:
    ticker: str
    side_suggested: str | None
    suggested_qty: float
    routed_qty_live: float
    filled_qty_live: float
    filled_usd_live: float
    drift_pp: float
    moved_toward_target: bool


@dataclass(frozen=True)
class WeekTrendRow:
    week_of: date
    suggested: int
    filled_live: int
    buy_filled_usd: float
    sell_filled_usd: float
    abs_drift_pp_sum: float


def compute_order_funnel(
    s: Session, *, mon: date, fri: date, broker_account_id: int
) -> OrderFunnel:
    """Count suggestion funnel states for the operations week (one broker account)."""
    row = s.execute(
        q.funnel_counts, {"mon": mon, "broker_account_id": broker_account_id}
    ).mappings().one()
    return OrderFunnel(
        week_of=mon,
        suggested=int(row["suggested"] or 0),
        accepted=int(row["accepted"] or 0),
        routed_live=int(row["routed_live"] or 0),
        filled_live=int(row["filled_live"] or 0),
        partial_live=int(row["partial_live"] or 0),
        accepted_not_routed=int(row["accepted_not_routed"] or 0),
        rejected=int(row["rejected"] or 0),
        expired=int(row["expired"] or 0),
        dry_run_count=int(row["dry_run_count"] or 0),
    )


def compute_order_flow(
    s: Session, *, mon: date, fri: date, broker_account_id: int
) -> OrderFlow:
    """Dollar notional of routed and filled orders (one broker account)."""
    row = s.execute(
        q.order_flow, {"mon": mon, "broker_account_id": broker_account_id}
    ).mappings().one()
    return OrderFlow(
        week_of=mon,
        buy_routed_usd=float(row["buy_routed_usd"] or 0),
        sell_routed_usd=float(row["sell_routed_usd"] or 0),
        buy_filled_usd=float(row["buy_filled_usd"] or 0),
        sell_filled_usd=float(row["sell_filled_usd"] or 0),
        dry_run_buy_usd=float(row["dry_run_buy_usd"] or 0),
        dry_run_sell_usd=float(row["dry_run_sell_usd"] or 0),
        distinct_tickers=int(row["distinct_tickers"] or 0),
    )


def compute_allocation_drift(
    s: Session, *, mon: date, fri: date, broker_account_id: int
) -> list[AllocationDriftRow]:
    """Per-ticker allocation drift Mon->Fri using positions_snapshot (one broker account).

    Scoped to broker_account_id so a ticker targeted in multiple accounts produces one
    drift row, not one per account.
    """
    rows = s.execute(
        q.alloc_drift,
        {"mon": str(mon), "fri": str(fri), "broker_account_id": broker_account_id},
    ).mappings().all()
    if not rows:
        return []

    # Detect targets_changed_midweek: any target_allocation row effective_from between (mon, fri]
    from sqlalchemy import text as _text
    midweek_count = s.execute(
        _text(
            "SELECT COUNT(*) FROM target_allocation "
            "WHERE effective_from > :mon AND effective_from <= :fri "
            "AND broker_account_id = :broker_account_id"
        ),
        {"mon": str(mon), "fri": str(fri), "broker_account_id": broker_account_id},
    ).scalar() or 0
    targets_changed_midweek = midweek_count > 0

    result = []
    for row in rows:
        current_pct_mon = float(row["current_pct_mon"] or 0)
        current_pct_fri = float(row["current_pct_fri"] or 0)
        target_pct = float(row["target_pct"] or 0)
        gap_pct_mon = target_pct - current_pct_mon
        gap_pct_fri = target_pct - current_pct_fri
        drift_pp = current_pct_fri - current_pct_mon
        moved_toward_target = abs(gap_pct_fri) < abs(gap_pct_mon)
        # monday_is_fallback: actual_mon_date (from SQL) != mon
        actual_mon_date_str = row["actual_mon_date"]
        monday_is_fallback = False
        if actual_mon_date_str is not None:
            if isinstance(actual_mon_date_str, date):
                monday_is_fallback = actual_mon_date_str != mon
            else:
                from datetime import datetime as _dt
                try:
                    actual = _dt.strptime(str(actual_mon_date_str), "%Y-%m-%d").date()
                    monday_is_fallback = actual != mon
                except Exception:
                    pass
        result.append(AllocationDriftRow(
            ticker=row["ticker"],
            target_pct=target_pct,
            current_pct_mon=current_pct_mon,
            current_pct_fri=current_pct_fri,
            gap_pct_mon=gap_pct_mon,
            gap_pct_fri=gap_pct_fri,
            drift_pp=drift_pp,
            moved_toward_target=moved_toward_target,
            targets_changed_midweek=targets_changed_midweek,
            monday_is_fallback=monday_is_fallback,
        ))
    return result


def compute_per_ticker_breakdown(
    s: Session, *, mon: date, fri: date, broker_account_id: int, top_n: int = 20
) -> list[PerTickerWeekRow]:
    """Per-ticker suggested/routed/filled breakdown (one broker account), enriched with drift."""
    rows = s.execute(
        q.per_ticker_breakdown, {"mon": mon, "broker_account_id": broker_account_id}
    ).mappings().all()
    if not rows:
        return []

    # Build drift lookup for drift_pp and moved_toward_target
    drift_map: dict[str, tuple[float, bool]] = {
        r.ticker: (r.drift_pp, r.moved_toward_target)
        for r in compute_allocation_drift(
            s, mon=mon, fri=fri, broker_account_id=broker_account_id
        )
    }

    result = []
    for row in rows:
        ticker = row["ticker"]
        drift_pp, moved = drift_map.get(ticker, (0.0, False))
        result.append(PerTickerWeekRow(
            ticker=ticker,
            side_suggested=row["side_suggested"],
            suggested_qty=float(row["suggested_qty"] or 0),
            routed_qty_live=float(row["routed_qty_live"] or 0),
            filled_qty_live=float(row["filled_qty_live"] or 0),
            filled_usd_live=float(row["filled_usd_live"] or 0),
            drift_pp=drift_pp,
            moved_toward_target=moved,
        ))

    # Cap at top_n by |drift_pp|
    result.sort(key=lambda r: abs(r.drift_pp), reverse=True)
    return result[:top_n]


def compute_4_week_trend(
    s: Session, *, current_fri: date, broker_account_id: int, trend_weeks: int = 4
) -> list[WeekTrendRow]:
    """4-week trend strip: one WeekTrendRow per week, oldest first (one broker account)."""
    # current_fri is a Friday; derive its Monday
    current_mon = current_fri - timedelta(days=current_fri.weekday())  # Monday of current week
    trend: list[WeekTrendRow] = []
    for weeks_back in range(trend_weeks - 1, -1, -1):  # oldest->newest
        mon = current_mon - timedelta(weeks=weeks_back)
        fri = mon + timedelta(days=4)
        try:
            funnel = compute_order_funnel(s, mon=mon, fri=fri, broker_account_id=broker_account_id)
            flow = compute_order_flow(s, mon=mon, fri=fri, broker_account_id=broker_account_id)
            drift = compute_allocation_drift(
                s, mon=mon, fri=fri, broker_account_id=broker_account_id
            )
            abs_drift_sum = sum(abs(r.drift_pp) for r in drift)
        except Exception as exc:
            logger.warning("compute_4_week_trend: failed for week %s: %s", mon, exc)
            continue
        trend.append(WeekTrendRow(
            week_of=mon,
            suggested=funnel.suggested,
            filled_live=funnel.filled_live + funnel.partial_live,
            buy_filled_usd=flow.buy_filled_usd,
            sell_filled_usd=flow.sell_filled_usd,
            abs_drift_pp_sum=abs_drift_sum,
        ))
    return trend
