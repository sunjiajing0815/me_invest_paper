"""Broker adapter protocol and shared dataclasses.

No file outside src/investor/brokers/ may import alpaca, moomoo, or any
broker SDK directly. All broker interaction flows through these abstractions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class Account:
    account_id: str
    cash_usd: float
    equity_usd: float
    buying_power_usd: float
    as_of: datetime


@dataclass(frozen=True)
class Position:
    ticker: str
    qty: float
    avg_cost: float
    market_value: float
    as_of: datetime


@dataclass(frozen=True)
class Activity:
    broker_order_id: str
    client_order_id: str | None
    ticker: str
    side: Literal["buy", "sell"]
    filled_qty: float
    filled_price: float
    filled_at: datetime
    status: Literal["filled", "partially_filled", "rejected", "expired", "canceled"]


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    ticker: str
    side: Literal["buy", "sell"]
    qty: float
    limit_price: float | None
    time_in_force: Literal["day", "gtc"] = "day"


@dataclass(frozen=True)
class OrderConfirmation:
    broker_order_id: str
    client_order_id: str
    status: str
    submitted_at: datetime


@runtime_checkable
class BrokerAdapter(Protocol):
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def get_activities(self, since: datetime, until: datetime | None = None) -> list[Activity]: ...
    def submit_order(self, req: OrderRequest) -> OrderConfirmation: ...
    def get_order(self, broker_order_id: str) -> OrderConfirmation: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
