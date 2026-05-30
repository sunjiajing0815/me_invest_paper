"""Tests for the per-account broker factory (Phase 4.9a B2)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.brokers import build_account_adapters, make_account_adapter
from investor.brokers.alpaca import AlpacaAdapter
from investor.models import Base, BrokerAccount

_NOW = datetime(2026, 5, 1, tzinfo=UTC)


def _fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        alpaca_api_key="settings-key",
        alpaca_secret_key="settings-secret",
        opend_host="host.docker.internal",
        opend_port=11111,
        opend_security_firm="FUTUSECURITIES",
    )


def test_make_account_adapter_alpaca_paper_vs_live() -> None:
    with patch("investor.brokers.alpaca.TradingClient") as mock_tc:
        paper_adapter = make_account_adapter(
            broker="alpaca", connection_config={"paper": True}, settings=_fake_settings()
        )
        assert isinstance(paper_adapter, AlpacaAdapter)
        assert mock_tc.call_args.kwargs["paper"] is True

        make_account_adapter(
            broker="alpaca", connection_config={"paper": False}, settings=_fake_settings()
        )
        assert mock_tc.call_args.kwargs["paper"] is False


def test_make_account_adapter_resolves_env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCT_X_KEY", "env-key")
    monkeypatch.setenv("ACCT_X_SECRET", "env-secret")
    with patch("investor.brokers.alpaca.TradingClient") as mock_tc:
        make_account_adapter(
            broker="alpaca",
            connection_config={
                "api_key_env": "ACCT_X_KEY",
                "secret_env": "ACCT_X_SECRET",
                "paper": True,
            },
            settings=_fake_settings(),
        )
    args, kwargs = mock_tc.call_args
    assert args[0] == "env-key" and args[1] == "env-secret"


def test_make_account_adapter_falls_back_to_settings_creds() -> None:
    with patch("investor.brokers.alpaca.TradingClient") as mock_tc:
        make_account_adapter(
            broker="alpaca", connection_config={"paper": True}, settings=_fake_settings()
        )
    args, _ = mock_tc.call_args
    assert args[0] == "settings-key" and args[1] == "settings-secret"


def test_make_account_adapter_unknown_broker_raises() -> None:
    with pytest.raises(NotImplementedError):
        make_account_adapter(broker="schwab", connection_config={}, settings=_fake_settings())


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _add_account(
    session: Session, *, account_ref: int, broker: str, connection_config: str | None
) -> None:
    session.add(
        BrokerAccount(
            account_ref=account_ref,
            account_id=f"acct-{account_ref}",
            broker=broker,
            mode="paper",
            nickname=f"Account {account_ref}",
            is_active=True,
            connection_config=connection_config,
            cash_usd=1000.0,
            equity_usd=1000.0,
            last_sync=_NOW,
            effective_from=_NOW,
            effective_to=None,
        )
    )
    session.flush()


def test_build_account_adapters_skips_unbuildable(db_session: Session) -> None:
    _add_account(db_session, account_ref=1, broker="alpaca", connection_config='{"paper": true}')
    _add_account(db_session, account_ref=2, broker="bogus", connection_config="{}")

    with patch("investor.brokers.alpaca.TradingClient"):
        adapters = build_account_adapters(db_session, _fake_settings())

    assert set(adapters) == {1}  # the bogus broker is logged + skipped, not raised
    assert isinstance(adapters[1], AlpacaAdapter)


def test_build_account_adapters_skips_inactive(db_session: Session) -> None:
    _add_account(db_session, account_ref=1, broker="alpaca", connection_config='{"paper": true}')
    # A soft-deleted account must not appear in the adapters dict.
    db_session.query(BrokerAccount).filter(BrokerAccount.account_ref == 1).update(
        {"is_active": False}
    )
    db_session.flush()

    with patch("investor.brokers.alpaca.TradingClient"):
        adapters = build_account_adapters(db_session, _fake_settings())

    assert adapters == {}
