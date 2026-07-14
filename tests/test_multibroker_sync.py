"""Regression tests for Phase 4.9a multi-broker sync + primary resolution.

Both bugs were found in the 2-broker smoke test (Alpaca + Moomoo):

* **Bug B** — ``resolve_primary_account_ref`` returned the *most-recently-synced*
  active account. Onboarding a new broker stamps ``last_sync = now``, so the new
  broker silently became "primary" — which every no-arg default (reads, jobs,
  auto-trade promote) then pointed at.
* **Bug A** — ``/admin/run-sync`` / the sync job always used the *primary* adapter
  and wrote to the *primary* account, so syncing broker B pulled broker A's
  positions and wrote them under B.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from investor.brokers.base import Account, Position
from investor.config import Settings
from investor.db import session_scope
from investor.jobs.sync import run_sync_all_brokers, run_sync_for_account
from investor.models import BrokerAccount
from investor.services.accounts import resolve_primary_account_ref

_ALPACA = 1  # lowest account_ref → the original / primary account
_MOOMOO = 2  # onboarded later, MORE recently synced (the Bug-B trap)




class _FakeAdapter:
    """Minimal BrokerAdapter stub returning a fixed account + position set."""

    def __init__(self, account_id: str, tickers: list[str], equity: float) -> None:
        self._account_id = account_id
        self._tickers = tickers
        self._equity = equity
        self._as_of = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)

    def get_account(self) -> Account:
        return Account(
            account_id=self._account_id,
            cash_usd=self._equity * 0.05,
            equity_usd=self._equity,
            buying_power_usd=self._equity,
            as_of=self._as_of,
        )

    def get_positions(self) -> list[Position]:
        return [
            Position(ticker=t, qty=10.0, avg_cost=100.0, market_value=1000.0, as_of=self._as_of)
            for t in self._tickers
        ]


def _seed_two_accounts(session: Session) -> None:
    """Alpaca (ref 1) synced earlier; Moomoo (ref 2) onboarded + synced LATER."""
    session.add(
        BrokerAccount(
            account_ref=_ALPACA, broker="alpaca", mode="paper", nickname="Alpaca",
            is_active=True, cash_usd=500.0, equity_usd=10_000.0,
            last_sync=datetime(2026, 5, 29, 21, 0, 0, tzinfo=UTC),
            effective_from=datetime(2026, 5, 1, tzinfo=UTC),
        )
    )
    session.add(
        BrokerAccount(
            account_ref=_MOOMOO, broker="moomoo", mode="paper", nickname="Moomoo",
            is_active=True, cash_usd=0.0, equity_usd=0.0,
            last_sync=datetime(2026, 5, 31, 11, 0, 0, tzinfo=UTC),  # more recent than Alpaca
            effective_from=datetime(2026, 5, 31, tzinfo=UTC),
        )
    )
    session.commit()


def _settings() -> Settings:
    return Settings(
        broker="alpaca_paper", alpaca_api_key="k", alpaca_secret_key="s",
        sqlite_path=":memory:", targets_path="config/targets.yaml",
    )


def _latest_tickers(ref: int) -> set[str]:
    """Tickers in the most recent snapshot for an account (qty > 0), via a fresh session."""
    with session_scope() as s:
        rows = s.execute(
            text(
                "SELECT ticker FROM positions_snapshot "
                "WHERE broker_account_id = :b AND qty > 0 "
                "AND ts = (SELECT MAX(ts) FROM positions_snapshot WHERE broker_account_id = :b)"
            ),
            {"b": ref},
        ).fetchall()
    return {r[0] for r in rows}


def test_primary_is_lowest_ref_not_most_recently_synced(db_session: Session) -> None:
    """Bug B: a more-recently-synced new broker must NOT displace the primary."""
    _seed_two_accounts(db_session)
    assert resolve_primary_account_ref(db_session) == _ALPACA


def test_run_sync_for_account_uses_the_given_adapter(db_session: Session) -> None:
    """Bug A: syncing one account writes that adapter's data under that account only."""
    _seed_two_accounts(db_session)
    run_sync_for_account(_FakeAdapter("MOOMOO-ACC", ["QQQ", "NVDA"], 5_000.0), _settings(), _MOOMOO)
    assert _latest_tickers(_MOOMOO) == {"QQQ", "NVDA"}
    assert _latest_tickers(_ALPACA) == set()  # untouched


def test_run_sync_all_brokers_routes_each_account_to_its_own_adapter(db_session: Session) -> None:
    """Bug A: no cross-contamination — each account gets its own adapter's positions."""
    _seed_two_accounts(db_session)
    adapters = {
        _ALPACA: _FakeAdapter("ALPACA-ACC", ["VOO", "SPY"], 10_000.0),
        _MOOMOO: _FakeAdapter("MOOMOO-ACC", ["QQQ", "NVDA"], 5_000.0),
    }
    run_sync_all_brokers(_settings(), adapters)
    assert _latest_tickers(_ALPACA) == {"VOO", "SPY"}
    assert _latest_tickers(_MOOMOO) == {"QQQ", "NVDA"}
