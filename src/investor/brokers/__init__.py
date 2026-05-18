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
    if settings.broker == "moomoo":
        from .moomoo import MoomooAdapter

        return MoomooAdapter(
            host=settings.opend_host,
            port=settings.opend_port,
            paper=False,
            security_firm=settings.opend_security_firm,
        )
    raise NotImplementedError(
        f"Broker {settings.broker!r} not yet implemented."
    )
