"""Regression tests: auto-trade write-path routing + broker-string single source of truth.

Both bugs traced to the back-compat ``run_auto_trade_job`` trusting *globals*
(``settings.broker`` and the *primary* account_ref) instead of the account it was asked
to trade:

* **Broker-string drift** — placement wrote ``settings.broker`` ("alpaca_paper") while
  ``persist_reconciliation`` wrote ``broker_account.broker`` ("alpaca"). When a fill
  arrived, the upsert (originally keyed on the broker string) missed the placement row
  and inserted a *duplicate* filled row, leaving the original stuck at
  ``accepted_for_routing`` — the GOOG ghost that the stale-order guard then treated as a
  still-open order and blocked legitimate orders against. ``5f8cf92`` made the match key
  tolerate the mismatch (broker_order_id + account); these tests prevent the mismatch
  being *written* in the first place, so ``order_execution.broker`` is self-consistent
  per account.
* **Cross-account misroute** — ``POST /admin/run-auto-trade?broker_account_id=N``
  resolved account N's adapter, then called ``run_auto_trade_job`` which *ignored* it and
  traded the *primary* account. So pointing the trigger at Moomoo (62) would run the
  primary's (61) suggestions through the Moomoo adapter. The account is now threaded
  through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.brokers.base import Account, OrderConfirmation, OrderRequest
from investor.config import Settings
from investor.db import override_engine_for_testing, session_scope
from investor.jobs.auto_trade import run_auto_trade_job, run_auto_trade_job_for_account
from investor.models import (
    AutoTradeCaps,
    AutoTradeState,
    Base,
    BrokerAccount,
    OrderExecution,
    OrderSuggestion,
)
from investor.services.accounts import AccountInfo

_NOW = datetime(2026, 5, 1, 9, 35, tzinfo=UTC)
# The job wrapper passes no as_of, so _fetch_accepted_unexecuted scopes to the *current*
# calendar week. Seed the suggestion in this week's Monday so it's eligible whenever the
# test runs (the eligibility window, not the assertion, is what we care about here).
_TODAY = datetime.now(UTC).date()
_WEEK = _TODAY - timedelta(days=_TODAY.weekday())  # this week's Monday
_ALPACA = 1  # primary (lowest active ref)
_MOOMOO = 2  # onboarded later


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    override_engine_for_testing(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


class _PlacingAdapter:
    """Adapter stub that accepts every order and reads it back cleanly (passes the
    LIVE readback: get_order().client_order_id == the submitted client_order_id)."""

    def __init__(self, order_id: str = "b-1") -> None:
        self._order_id = order_id
        self._last_client_oid: str | None = None

    def get_account(self) -> Account:
        return Account(
            account_id="x", cash_usd=100_000.0, equity_usd=100_000.0,
            buying_power_usd=100_000.0, as_of=_NOW,
        )

    def submit_order(self, req: OrderRequest) -> OrderConfirmation:
        self._last_client_oid = req.client_order_id
        return OrderConfirmation(
            broker_order_id=self._order_id, client_order_id=req.client_order_id,
            status="accepted", submitted_at=_NOW,
        )

    def get_order(self, order_id: str) -> OrderConfirmation:
        return OrderConfirmation(
            broker_order_id=order_id, client_order_id=self._last_client_oid,
            status="accepted", submitted_at=_NOW,
        )


def _seed_account(session: Session, ref: int, broker: str, nickname: str) -> None:
    session.add(
        BrokerAccount(
            account_ref=ref, account_id=f"acc-{ref}", broker=broker, mode="paper",
            nickname=nickname, is_active=True, cash_usd=100_000.0, equity_usd=100_000.0,
            last_sync=_NOW, effective_from=_NOW, effective_to=None,
        )
    )
    session.add(AutoTradeState(broker_account_id=ref, mode="LIVE"))


def _seed_caps(session: Session) -> None:
    session.add(
        AutoTradeCaps(
            per_order_max_usd=500.0, per_day_max_usd=1500.0,
            per_week_max_usd_per_ticker=1000.0, per_day_max_orders=5,
            effective_from=_NOW - timedelta(hours=1), effective_to=None,
        )
    )


def _seed_suggestion(session: Session, ref: int, ticker: str = "AAPL") -> None:
    session.add(
        OrderSuggestion(
            broker_account_id=ref, week_of=_WEEK, ticker=ticker, side="buy",
            qty=1.0, limit_price=100.0, reason="test", status="accepted",
            created_at=_NOW - timedelta(hours=1),
        )
    )


def _settings() -> Settings:
    # broker="alpaca_paper" deliberately DIFFERS from broker_account.broker ("alpaca")
    # so a regression to settings.broker would surface as a drifting write.
    return Settings(
        broker="alpaca_paper", alpaca_api_key="k", alpaca_secret_key="s",
        sqlite_path=":memory:", targets_path="config/targets.yaml", email_to="t@t.com",
    )


def _real_executions() -> list[dict[str, object]]:
    """Plain-value snapshot of real (non-dry-run) executions — extracted inside the
    session so callers never touch a detached ORM instance (CLAUDE.md rule 9)."""
    with session_scope() as s:
        rows = s.scalars(
            select(OrderExecution).where(OrderExecution.dry_run.is_(False))
        ).all()
        return [
            {
                "broker": e.broker,
                "broker_account_id": e.broker_account_id,
                "status": e.status,
                "ticker": e.ticker,
            }
            for e in rows
        ]


def test_placement_writes_account_broker_not_settings_broker(db_session: Session) -> None:
    """The broker string on the placed row is the account's family ("alpaca"), the same
    string reconciliation writes — never settings.broker ("alpaca_paper")."""
    _seed_account(db_session, _ALPACA, "alpaca", "Alpaca")
    _seed_caps(db_session)
    _seed_suggestion(db_session, _ALPACA)
    db_session.commit()  # the job opens its own session — seeds must be committed

    run_auto_trade_job_for_account(
        _settings(), _PlacingAdapter(), MagicMock(),
        AccountInfo(account_ref=_ALPACA, nickname="Alpaca", broker="alpaca"),
    )

    rows = _real_executions()
    assert len(rows) == 1  # placed exactly once, no duplicate
    assert rows[0]["broker"] == "alpaca"  # account.broker, NOT "alpaca_paper"
    assert rows[0]["broker_account_id"] == _ALPACA
    assert rows[0]["status"] == "accepted_for_routing"


def test_run_auto_trade_job_trades_the_requested_account_not_the_primary(
    db_session: Session,
) -> None:
    """Both accounts have an accepted suggestion; triggering for the non-primary account
    must place the NON-primary's order only (the misroute placed the primary's)."""
    _seed_account(db_session, _ALPACA, "alpaca", "Alpaca")
    _seed_account(db_session, _MOOMOO, "moomoo", "Moomoo")
    _seed_caps(db_session)
    _seed_suggestion(db_session, _ALPACA, ticker="VOO")
    _seed_suggestion(db_session, _MOOMOO, ticker="NVDA")
    db_session.commit()

    run_auto_trade_job(
        _settings(), _PlacingAdapter(), MagicMock(),
        account=AccountInfo(account_ref=_MOOMOO, nickname="Moomoo", broker="moomoo"),
    )

    rows = _real_executions()
    assert len(rows) == 1
    assert rows[0]["broker_account_id"] == _MOOMOO  # the requested account, not primary 1
    assert rows[0]["broker"] == "moomoo"
    assert rows[0]["ticker"] == "NVDA"
