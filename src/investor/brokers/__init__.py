"""Broker adapter factory.

Only this module and the individual adapter modules may import broker SDKs.
"""

from __future__ import annotations

import logging

from ..config import Settings
from .alpaca import AlpacaAdapter
from .base import BrokerAdapter

logger = logging.getLogger(__name__)


def make_adapter(settings: Settings) -> BrokerAdapter:
    """Return the correct adapter for the configured broker."""
    if settings.broker == "alpaca_paper":
        return AlpacaAdapter(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=True,
        )
    if settings.broker == "alpaca_live":
        return AlpacaAdapter(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=False,
        )
    raise NotImplementedError(
        f"Broker {settings.broker!r} not yet implemented."
    )
