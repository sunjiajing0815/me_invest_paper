"""Alpaca broker adapter wrapping alpaca-py TradingClient.

Converts Alpaca SDK types to domain dataclasses at the boundary.
alpaca-py returns numeric fields as strings — every numeric is wrapped in float().
ALPACA_BASE_URL env var is intentionally ignored; routing is via paper=True flag.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from alpaca.trading.client import TradingClient

from .base import Account, Position

logger = logging.getLogger(__name__)


class AlpacaAdapter:
    def __init__(self, api_key: str, secret_key: str, *, paper: bool) -> None:
        self._client = TradingClient(api_key, secret_key, paper=paper)
        logger.info("AlpacaAdapter initialised in %s mode", "paper" if paper else "live")

    def get_account(self) -> Account:
        raw = self._client.get_account()
        return Account(
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
