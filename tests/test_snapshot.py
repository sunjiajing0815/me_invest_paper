"""take_snapshot must tag every row in one sync with a single ts (the drift SQL selects a
snapshot batch via ts = MAX(ts); per-row timestamps break it — the Moomoo drift-zeros bug)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from investor.brokers.base import Account, Position
from investor.models import PositionsSnapshot
from investor.services.snapshot import take_snapshot


def _adapter() -> tuple[Any, Account]:
    acct_ts = datetime(2026, 6, 12, 21, 1, 40, tzinfo=UTC)
    account = Account(account_id="moomoo", cash_usd=1000.0, equity_usd=10000.0,
                      buying_power_usd=1000.0, as_of=acct_ts, currency="USD")
    # Distinct microsecond as_of per position — mimics the Moomoo adapter's per-row now().
    base = datetime(2026, 6, 12, 21, 1, 40, 100000, tzinfo=UTC)
    positions = [
        Position(ticker="QQQ", qty=2.0, avg_cost=600.0, market_value=1300.0,
                 as_of=base + timedelta(microseconds=1), currency="USD"),
        Position(ticker="BTC", qty=30.0, avg_cost=30.0, market_value=800.0,
                 as_of=base + timedelta(microseconds=2), currency="USD"),
        Position(ticker="AAPL", qty=0.01, avg_cost=200.0, market_value=2.0,
                 as_of=base + timedelta(microseconds=3), currency="USD"),
    ]
    adapter = SimpleNamespace(get_account=lambda: account, get_positions=lambda: positions)
    return adapter, account


def test_snapshot_rows_share_one_ts(session: Session) -> None:
    adapter, account = _adapter()
    settings = SimpleNamespace(broker="moomoo")
    take_snapshot(adapter, session, settings, broker_account_id=62)

    rows = session.scalars(
        select(PositionsSnapshot).where(PositionsSnapshot.broker_account_id == 62)
    ).all()
    assert len(rows) == 3
    timestamps = {r.ts for r in rows}
    assert len(timestamps) == 1, f"expected one shared ts per sync, got {len(timestamps)}"
