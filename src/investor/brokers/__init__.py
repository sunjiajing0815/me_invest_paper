"""Broker adapter factory.

Only this module and the individual adapter modules may import broker SDKs.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import BrokerAccount
from .alpaca import AlpacaAdapter
from .base import BrokerAdapter

logger = logging.getLogger(__name__)


def make_adapter(settings: Settings) -> BrokerAdapter:
    """Return the correct adapter for the configured broker.

    Paper-only build: `alpaca_paper` is the only accepted broker (enforced again
    in config.VALID_BROKERS). See src/investor/safety.py.
    """
    if settings.broker == "alpaca_paper":
        return AlpacaAdapter(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=True,
        )
    raise NotImplementedError(
        f"Broker {settings.broker!r} is not available in this paper-only build."
    )


def make_account_adapter(
    *, broker: str, connection_config: dict[str, Any], settings: Settings
) -> BrokerAdapter:
    """Build the adapter for one broker account from its broker + connection_config.

    Unlike ``make_adapter`` (which keys on ``settings.broker``), this builds from a
    ``broker_account`` row: ``broker`` is the bare adapter family ("alpaca")
    and ``connection_config`` is the JSON blob naming credentials / connection params.
    Env-var names in the config are resolved via ``os.environ``, falling back to the
    matching ``settings`` value so Jane's existing single-broker setup keeps working.
    """
    if broker == "alpaca":
        # L2: `connection_config["paper"]` is deliberately ignored. The admin API
        # (POST /admin/broker-accounts) is a second door into adapter construction,
        # and it must not be able to request a live account. See src/investor/safety.py.
        api_key = os.environ.get(
            connection_config.get("api_key_env", ""), settings.alpaca_api_key
        )
        secret = os.environ.get(
            connection_config.get("secret_env", ""), settings.alpaca_secret_key
        )
        return AlpacaAdapter(api_key=api_key, secret_key=secret, paper=True)
    raise NotImplementedError(
        f"Broker {broker!r} is not available in this paper-only build."
    )


def build_account_adapters(
    session: Session, settings: Settings
) -> dict[int, BrokerAdapter]:
    """Return ``{account_ref: adapter}`` for every active broker account.

    One adapter per distinct ``account_ref`` (latest open row wins). An account whose
    adapter fails to construct (bad/missing config, SDK not installed) is logged and
    skipped so one broken broker doesn't crash startup or abort the per-broker loops.
    """
    adapters: dict[int, BrokerAdapter] = {}
    rows = session.scalars(
        select(BrokerAccount)
        .where(BrokerAccount.effective_to.is_(None), BrokerAccount.is_active.is_(True))
        .order_by(BrokerAccount.last_sync.desc())
    ).all()
    for r in rows:
        if r.account_ref is None or r.account_ref in adapters:
            continue
        try:
            cfg = json.loads(r.connection_config) if r.connection_config else {}
            adapters[r.account_ref] = make_account_adapter(
                broker=r.broker, connection_config=cfg, settings=settings
            )
        except Exception as exc:
            logger.error(
                "build_account_adapters: account_ref=%s (%s) failed to build: %s",
                r.account_ref, r.nickname, exc,
            )
    return adapters
