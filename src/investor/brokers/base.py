"""Broker adapter protocol and shared dataclasses.

No file outside src/investor/brokers/ may import alpaca, moomoo, or any
broker SDK directly. All broker interaction flows through these abstractions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Account:
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


@runtime_checkable
class BrokerAdapter(Protocol):
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
