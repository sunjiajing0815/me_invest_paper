"""Alpaca broker adapter wrapping alpaca-py TradingClient.

Converts Alpaca SDK types to domain dataclasses at the boundary.
alpaca-py returns numeric fields as strings — every numeric is wrapped in float().
Paper/live routing is via paper=True only; URL override is not supported.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import cast

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from .base import Account, Activity, OrderConfirmation, OrderRequest, Position

logger = logging.getLogger(__name__)


class AlpacaAdapter:
    def __init__(self, api_key: str, secret_key: str, *, paper: bool) -> None:
        self._client = TradingClient(api_key, secret_key, paper=paper)
        logger.info("AlpacaAdapter initialised in %s mode", "paper" if paper else "live")

    def get_account(self) -> Account:
        raw = self._client.get_account()
        return Account(
            account_id=str(raw.id),
            cash_usd=float(raw.cash),
            equity_usd=float(raw.equity),
            buying_power_usd=float(raw.buying_power),
            as_of=datetime.now(UTC),
        )

    def get_positions(self) -> list[Position]:
        now = datetime.now(UTC)
        raw_positions = self._client.get_all_positions()
        positions = [
            Position(
                ticker=p.symbol,
                qty=float(p.qty),
                avg_cost=float(p.avg_entry_price),
                market_value=float(p.market_value),
                as_of=now,
            )
            for p in raw_positions
        ]
        logger.info("Fetched %d positions from Alpaca", len(positions))
        return positions

    def get_activities(self, since: datetime, until: datetime | None = None) -> list[Activity]:
        """Return closed orders with fills since *since*.

        Applies a 1-hour lookback overlap to handle timezone edge cases;
        the unique DB constraint deduplicates any overlap.
        """
        # Pull with 1-hour overlap to handle timezone edge cases; unique constraint deduplicates
        effective_since = since - timedelta(hours=1)

        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            after=effective_since,
            until=until,
            limit=500,  # max per Alpaca docs
        )
        orders = self._client.get_orders(filter=req)

        status_map: dict[str, str] = {
            "filled": "filled",
            "partially_filled": "partially_filled",
            "rejected": "rejected",
            "expired": "expired",
            "canceled": "canceled",
        }

        activities: list[Activity] = []
        for _order in orders:
            order = cast(AlpacaOrder, _order)
            # Only include orders with actual fills
            if not order.filled_qty or float(order.filled_qty) <= 0:
                continue
            act_status = status_map.get(str(order.status.value), "expired")
            activities.append(
                Activity(
                    broker_order_id=str(order.id),
                    client_order_id=order.client_order_id,
                    ticker=str(order.symbol),
                    side="buy" if order.side == OrderSide.BUY else "sell",
                    filled_qty=float(order.filled_qty),
                    filled_price=float(order.filled_avg_price or 0),
                    filled_at=order.filled_at or datetime.now(UTC),
                    status=act_status,  # type: ignore[arg-type]
                )
            )
        logger.info("Fetched %d activities from Alpaca since %s", len(activities), since)
        return activities

    def submit_order(self, req: OrderRequest) -> OrderConfirmation:
        """Submit a limit order to Alpaca and return the broker confirmation."""
        _order = self._client.submit_order(
            LimitOrderRequest(
                symbol=req.ticker,
                qty=req.qty,
                side=OrderSide.BUY if req.side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY if req.time_in_force == "day" else TimeInForce.GTC,
                limit_price=req.limit_price,
                client_order_id=req.client_order_id,
            )
        )
        order = cast(AlpacaOrder, _order)
        return OrderConfirmation(
            broker_order_id=str(order.id),
            client_order_id=order.client_order_id or req.client_order_id,
            status=str(order.status.value),
            submitted_at=order.submitted_at or datetime.now(UTC),
        )

    def get_order(self, broker_order_id: str) -> OrderConfirmation:
        """Fetch a single order by its Alpaca order ID."""
        order = cast(AlpacaOrder, self._client.get_order_by_id(broker_order_id))
        return OrderConfirmation(
            broker_order_id=str(order.id),
            client_order_id=order.client_order_id or "",
            status=str(order.status.value),
            submitted_at=order.submitted_at or datetime.now(UTC),
        )

    def cancel_order(self, broker_order_id: str) -> None:
        """Cancel an open order by its Alpaca order ID."""
        self._client.cancel_order_by_id(broker_order_id)
