"""Daily report composer: reads DB, returns an immutable DailyReport dataclass."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BrokerAccount, OrderExecution, OrderSuggestion
from ..queries import positions_latest
from .charts import ALLOC_PALETTE, CASH_COLOR, OTHER_COLOR
from .gap import GapRow, UntrackedPosition, compute_gap, get_untracked_positions
from .indicators import IndicatorRow
from .levels import NearbyLevels

# How many position slices to show individually before grouping the rest into "Other".
_ALLOC_TOP_N = 8


@dataclass(frozen=True)
class AccountSnapshot:
    """Plain-data copy of a BrokerAccount row — safe to use after the session closes."""

    broker: str
    mode: str
    cash_usd: float
    equity_usd: float
    currency: str = "USD"  # base currency of cash_usd / equity_usd


def _account_currency(connection_config: str | None) -> str:
    """Base currency from a broker_account's connection_config JSON (default USD)."""
    try:
        return str(json.loads(connection_config or "{}").get("currency", "USD"))
    except (ValueError, TypeError):
        return "USD"


@dataclass(frozen=True)
class CommittedOrderRow:
    """A this-week accepted suggestion + its broker order state (for the daily email)."""

    sid: int
    ticker: str
    side: str
    qty: float
    limit_price: float
    status_label: str          # Working | Partially filled | Filled | Awaiting placement
    filled_price: float | None
    cancellable: bool          # un-accept link shown only when True


def _committed_status(exe: Any | None) -> tuple[str, bool]:
    """Map the latest real execution to (status_label, cancellable)."""
    if exe is None:
        return "Awaiting placement", True
    st = exe.status
    if st == "accepted_for_routing":
        return "Working", True
    if st == "partially_filled":
        return "Partially filled", True
    if st == "filled":
        return "Filled", False
    if st == "broker_cancelled":
        return "Order cancelled", True
    return st, False


@dataclass(frozen=True)
class FillRow:
    """A single this-week fill (for the daily order-activity summary)."""

    ticker: str
    side: str
    filled_qty: float
    filled_price: float | None
    filled_at: datetime | None


@dataclass(frozen=True)
class OrdersThisWeek:
    """Summary of real orders placed and filled since Monday (replaces the levels table)."""

    placed_count: int
    filled_count: int
    filled_notional_usd: float
    fills: list[FillRow] = field(default_factory=list)


@dataclass(frozen=True)
class AllocationSlice:
    """One slice of the allocation donut (a position, 'Other', or 'Cash')."""

    label: str
    value_usd: float
    pct: float
    color: str  # hex, shared by the PNG donut and the HTML legend


def _build_allocation_slices(
    positions: list[Any], cash_usd: float
) -> list[AllocationSlice]:
    """Distribution of equity across positions + cash (values assumed USD).

    Largest positions first; positions beyond the top N fold into 'Other'; cash
    (when positive) is always the final slice. Colours come from the shared
    chart palette so the donut and legend never drift."""
    holdings = sorted(
        ((str(p.ticker), float(p.market_value)) for p in positions if p.market_value > 0),
        key=lambda x: x[1],
        reverse=True,
    )
    pos_total = sum(mv for _, mv in holdings)
    cash = cash_usd if cash_usd > 0 else 0.0
    total = pos_total + cash
    if total <= 0:
        return []

    slices: list[AllocationSlice] = []
    top = holdings[:_ALLOC_TOP_N]
    rest = holdings[_ALLOC_TOP_N:]
    for i, (ticker, mv) in enumerate(top):
        slices.append(AllocationSlice(ticker, mv, mv / total * 100, ALLOC_PALETTE[i]))
    if rest:
        other_mv = sum(mv for _, mv in rest)
        slices.append(AllocationSlice("Other", other_mv, other_mv / total * 100, OTHER_COLOR))
    if cash > 0:
        slices.append(AllocationSlice("Cash", cash, cash / total * 100, CASH_COLOR))
    return slices


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
    committed_orders: list[CommittedOrderRow] = field(default_factory=list)
    orders_this_week: OrdersThisWeek = field(
        default_factory=lambda: OrdersThisWeek(0, 0, 0.0, [])
    )
    allocation_slices: list[AllocationSlice] = field(default_factory=list)


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
            currency=_account_currency(orm_account.connection_config),
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

    # This-week accepted suggestions + their latest real execution = "committed orders".
    week_monday = today - timedelta(days=today.weekday())
    accepted_sugs = session.scalars(
        select(OrderSuggestion).where(
            OrderSuggestion.broker_account_id == broker_account_id,
            OrderSuggestion.status == "accepted",
            OrderSuggestion.week_of == week_monday,
        ).order_by(OrderSuggestion.ticker)
    ).all()
    committed: list[CommittedOrderRow] = []
    for sug in accepted_sugs:
        exe = session.scalars(
            select(OrderExecution).where(
                OrderExecution.suggestion_id == sug.id,
                OrderExecution.dry_run.is_(False),
            ).order_by(OrderExecution.created_at.desc())
        ).first()
        label, cancellable = _committed_status(exe)
        committed.append(CommittedOrderRow(
            sid=sug.id, ticker=sug.ticker, side=sug.side, qty=sug.qty,
            limit_price=sug.limit_price, status_label=label,
            filled_price=(exe.filled_price if exe else None), cancellable=cancellable,
        ))

    # Orders placed and filled this week (real executions only) — the daily activity recap.
    week_start = datetime.combine(week_monday, datetime.min.time(), tzinfo=UTC)
    week_exes = session.scalars(
        select(OrderExecution).where(
            OrderExecution.broker_account_id == broker_account_id,
            OrderExecution.dry_run.is_(False),
            OrderExecution.created_at >= week_start,
        )
    ).all()
    filled = [e for e in week_exes if e.filled_qty and e.filled_qty > 0]
    fills = sorted(
        (
            FillRow(
                ticker=e.ticker, side=e.side, filled_qty=e.filled_qty,
                filled_price=e.filled_price, filled_at=e.filled_at,
            )
            for e in filled
        ),
        key=lambda f: (f.filled_at is not None, f.filled_at),
        reverse=True,
    )
    orders_this_week = OrdersThisWeek(
        placed_count=len(week_exes),
        filled_count=len(filled),
        filled_notional_usd=sum(
            e.filled_qty * e.filled_price for e in filled if e.filled_price is not None
        ),
        fills=fills,
    )

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
        committed_orders=committed,
        orders_this_week=orders_this_week,
        allocation_slices=_build_allocation_slices(
            list(positions), account.cash_usd if account else 0.0
        ),
    )
