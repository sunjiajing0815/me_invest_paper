"""Tests for POST /admin/reset-week-buy-suggestions (side query parameter).

These tests use a minimal patching strategy:
- Patch ``init_db`` to initialise an in-memory SQLite engine instead.
- Patch ``load_targets`` to return a dummy targets object.
- Patch ``update_bars``, ``make_adapter``, ``SMTPEmailer``, ``make_scheduler``,
  and the remaining service factories so startup doesn't touch the network.
- Override the ``_get_settings`` FastAPI dependency to inject a Settings stub
  with a known ADMIN_TOKEN.
- Inject a mock adapter onto ``app.state.adapter`` before each request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing
from investor.models import Base, BrokerAccount, OrderSuggestion
from investor.services.suggest import _next_monday

_ADMIN_TOKEN = "test-admin-token"
_WEEK = _next_monday()
_ACCT = 1  # account_ref of the seeded active account

# ── shared in-memory engine (module-level so patched init_db can store it) ──

_TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    poolclass=StaticPool,
    connect_args={"check_same_thread": False},
    future=True,
)
Base.metadata.create_all(_TEST_ENGINE)
override_engine_for_testing(_TEST_ENGINE)


def _fake_init_db(sqlite_path: str):  # noqa: ARG001
    """Replace init_db — reinstate the test engine in case another test replaced it."""
    override_engine_for_testing(_TEST_ENGINE)
    return _TEST_ENGINE


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_db():
    """Truncate all tables before each test so they start clean."""
    from sqlalchemy import text

    with _TEST_ENGINE.begin() as conn:
        for tbl in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f"DELETE FROM {tbl.name}"))  # noqa: S608
    # Seed the active primary broker account that the endpoints resolve against.
    with Session(_TEST_ENGINE) as s:
        s.add(BrokerAccount(
            account_ref=_ACCT, broker="alpaca", mode="paper", is_active=True,
            cash_usd=1000.0, equity_usd=1000.0,
            last_sync=datetime.now(UTC), effective_from=datetime.now(UTC),
        ))
        s.commit()


@pytest.fixture()
def db_session() -> Session:
    with Session(_TEST_ENGINE) as session:
        yield session


@pytest.fixture()
def mock_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.cancel_order.return_value = None
    return adapter


@pytest.fixture()
def client(mock_adapter):
    """TestClient for the investor app with startup side-effects patched out."""
    fake_settings = MagicMock()
    fake_settings.admin_token = _ADMIN_TOKEN
    fake_settings.broker = "alpaca_paper"
    fake_settings.log_level = "INFO"
    fake_settings.sqlite_path = ":memory:"
    fake_settings.targets_path = "config/targets.yaml"
    fake_settings.bars_dir = "data/bars"
    fake_settings.alpaca_api_key = "test"
    fake_settings.alpaca_secret_key = "test"

    fake_targets = MagicMock()
    fake_targets.targets = {}
    fake_targets.watchlist = []

    fake_scheduler = MagicMock()

    patches = [
        patch("investor.main.init_db", side_effect=_fake_init_db),
        patch("investor.main.load_targets", return_value=fake_targets),
        patch("investor.main.make_adapter", return_value=mock_adapter),
        patch("investor.main.SMTPEmailer", return_value=MagicMock()),
        patch("investor.main.make_scheduler", return_value=fake_scheduler),
        patch("investor.services.bars.update_bars"),
        patch("investor.main.Settings", return_value=fake_settings),
    ]

    # Some factories are imported inside lifespan — patch them there too.
    extra_patches = [
        patch("investor.services.llm.make_llm_client", return_value=MagicMock()),
        patch("investor.services.tavily.make_tavily_client", return_value=MagicMock()),
        patch("investor.services.earnings.make_earnings_client", return_value=MagicMock()),
        patch("investor.services.sentiment.make_sentiment_client", return_value=MagicMock()),
    ]

    from investor.main import _get_settings, app

    app.dependency_overrides[_get_settings] = lambda: fake_settings

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        extra_patches[0],
        extra_patches[1],
        extra_patches[2],
        extra_patches[3],TestClient(app, raise_server_exceptions=True) as tc
    ):
        app.state.adapter = mock_adapter
        app.state.adapters = {_ACCT: mock_adapter}
        yield tc

    app.dependency_overrides.clear()


# ── helpers ───────────────────────────────────────────────────────────────────


def _add_suggestion(
    session: Session,
    *,
    ticker: str,
    side: str,
    status: str = "accepted",
) -> OrderSuggestion:
    sug = OrderSuggestion(
        broker_account_id=_ACCT,
        week_of=_WEEK,
        ticker=ticker,
        side=side,
        qty=2.0,
        limit_price=100.0,
        reason="test",
        status=status,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(sug)
    session.flush()
    return sug


def _post_reset(client: TestClient, *, side: str | None = None, url: str | None = None) -> dict:
    if url is None:
        url = "/admin/reset-week-buy-suggestions"
    if side is not None:
        url = f"{url}?side={side}"
    resp = client.post(url, headers={"X-Admin-Token": _ADMIN_TOKEN})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── default behaviour (side=buy) ──────────────────────────────────────────────


class TestResetSideBuyDefault:
    def test_buy_suggestion_reset_to_pending(self, client, db_session):
        sug = _add_suggestion(db_session, ticker="AAPL", side="buy")
        db_session.commit()

        data = _post_reset(client)

        db_session.refresh(sug)
        assert sug.status == "pending"
        assert sug.id in data["suggestions_reset"]

    def test_sell_suggestion_untouched_by_default(self, client, db_session):
        sell_sug = _add_suggestion(db_session, ticker="AAPL", side="sell")
        db_session.commit()

        data = _post_reset(client)

        db_session.refresh(sell_sug)
        assert sell_sug.status == "accepted"
        assert sell_sug.id not in data["suggestions_reset"]


# ── side=sell ─────────────────────────────────────────────────────────────────


class TestResetSideSell:
    def test_sell_suggestion_reset_to_pending(self, client, db_session):
        sell_sug = _add_suggestion(db_session, ticker="MSFT", side="sell")
        db_session.commit()

        data = _post_reset(client, side="sell")

        db_session.refresh(sell_sug)
        assert sell_sug.status == "pending"
        assert sell_sug.id in data["suggestions_reset"]

    def test_buy_suggestion_not_touched_when_side_sell(self, client, db_session):
        buy_sug = _add_suggestion(db_session, ticker="AAPL", side="buy")
        db_session.commit()

        data = _post_reset(client, side="sell")

        db_session.refresh(buy_sug)
        assert buy_sug.status == "accepted"
        assert buy_sug.id not in data["suggestions_reset"]


# ── side=all ──────────────────────────────────────────────────────────────────


class TestResetSideAll:
    def test_both_buy_and_sell_reset_when_side_all(self, client, db_session):
        buy_sug = _add_suggestion(db_session, ticker="AAPL", side="buy")
        # Ticker differs to satisfy the unique constraint (week_of, ticker, side)
        sell_sug = _add_suggestion(db_session, ticker="MSFT", side="sell")
        db_session.commit()

        data = _post_reset(client, side="all")

        db_session.refresh(buy_sug)
        db_session.refresh(sell_sug)
        assert buy_sug.status == "pending"
        assert sell_sug.status == "pending"
        assert buy_sug.id in data["suggestions_reset"]
        assert sell_sug.id in data["suggestions_reset"]

    def test_side_all_resets_multiple_tickers(self, client, db_session):
        sugs = [
            _add_suggestion(db_session, ticker="AAPL", side="buy"),
            _add_suggestion(db_session, ticker="MSFT", side="buy"),
            _add_suggestion(db_session, ticker="TSLA", side="sell"),
        ]
        db_session.commit()

        data = _post_reset(client, side="all")

        for sug in sugs:
            db_session.refresh(sug)
            assert sug.status == "pending"
            assert sug.id in data["suggestions_reset"]


# ── invalid side value → 422 ──────────────────────────────────────────────────


class TestResetInvalidSide:
    def test_invalid_side_returns_422(self, client):
        resp = client.post(
            "/admin/reset-week-buy-suggestions?side=trim",
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )
        assert resp.status_code == 422


# ── canonical endpoint ────────────────────────────────────────────────────────


class TestCanonicalEndpoint:
    def test_canonical_endpoint_resets_buy_by_default(self, client, db_session):
        """Test that the new canonical /admin/reset-week-suggestions endpoint works."""
        sug = _add_suggestion(db_session, ticker="AAPL", side="buy")
        db_session.commit()

        data = _post_reset(client, url="/admin/reset-week-suggestions")

        db_session.refresh(sug)
        assert sug.status == "pending"
        assert sug.id in data["suggestions_reset"]

    def test_canonical_endpoint_with_side_sell(self, client, db_session):
        """Test canonical endpoint with side=sell parameter."""
        sell_sug = _add_suggestion(db_session, ticker="MSFT", side="sell")
        db_session.commit()

        data = _post_reset(client, side="sell", url="/admin/reset-week-suggestions")

        db_session.refresh(sell_sug)
        assert sell_sug.status == "pending"
        assert sell_sug.id in data["suggestions_reset"]

    def test_canonical_endpoint_with_side_all(self, client, db_session):
        """Test canonical endpoint with side=all parameter."""
        buy_sug = _add_suggestion(db_session, ticker="AAPL", side="buy")
        sell_sug = _add_suggestion(db_session, ticker="MSFT", side="sell")
        db_session.commit()

        data = _post_reset(client, side="all", url="/admin/reset-week-suggestions")

        db_session.refresh(buy_sug)
        db_session.refresh(sell_sug)
        assert buy_sug.status == "pending"
        assert sell_sug.status == "pending"
        assert buy_sug.id in data["suggestions_reset"]
        assert sell_sug.id in data["suggestions_reset"]


# ── B-API: broker_account_id scoping + 404 validation ────────────────────────


class TestBApiScope:
    def test_health_lists_active_accounts(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        refs = {a["broker_account_id"] for a in body["accounts"]}
        assert _ACCT in refs

    def test_gap_invalid_account_returns_404(self, client):
        resp = client.get("/gap?broker_account_id=999")
        assert resp.status_code == 404

    def test_gap_primary_default_ok(self, client):
        resp = client.get("/gap")  # no param → primary account
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_run_daily_report_invalid_account_returns_404(self, client):
        resp = client.post(
            "/admin/run-daily-report?broker_account_id=999",
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )
        assert resp.status_code == 404

    def test_emergency_stop_invalid_account_returns_404(self, client):
        resp = client.post(
            "/admin/auto-trade/emergency-stop?broker_account_id=999",
            headers={"X-Admin-Token": _ADMIN_TOKEN},
        )
        assert resp.status_code == 404


# ── Stage C: broker-account onboarding ───────────────────────────────────────


class TestBrokerAccounts:
    _hdr = {"X-Admin-Token": _ADMIN_TOKEN}

    def test_create_then_list_and_health(self, client):
        resp = client.post(
            "/admin/broker-accounts",
            headers=self._hdr,
            json={"broker": "alpaca", "nickname": "Second", "connection_config": {"paper": True}},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        new_ref = body["broker_account_id"]
        assert body["auto_trade_mode"] == "OFF"
        assert isinstance(new_ref, int) and new_ref != _ACCT

        # New account is registered live in app.state.adapters.
        from investor.main import app
        assert new_ref in app.state.adapters

        # Both accounts show up in the listing and in /health.
        listing = client.get("/admin/broker-accounts", headers=self._hdr).json()
        assert {_ACCT, new_ref} <= {a["broker_account_id"] for a in listing}
        health = client.get("/health").json()["accounts"]
        assert {_ACCT, new_ref} <= {a["broker_account_id"] for a in health}

    def test_create_unsupported_broker_400(self, client):
        resp = client.post(
            "/admin/broker-accounts",
            headers=self._hdr,
            json={"broker": "schwab", "nickname": "Nope", "connection_config": {}},
        )
        assert resp.status_code == 400

    def test_soft_delete_excludes_from_health_but_keeps_history(self, client):
        new_ref = client.post(
            "/admin/broker-accounts",
            headers=self._hdr,
            json={"broker": "alpaca", "nickname": "Temp", "connection_config": {"paper": True}},
        ).json()["broker_account_id"]

        resp = client.request("DELETE", f"/admin/broker-accounts/{new_ref}", headers=self._hdr)
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

        # Excluded from /health (active loops) but still listed (history) as inactive.
        health = client.get("/health").json()["accounts"]
        assert new_ref not in {a["broker_account_id"] for a in health}
        listing = client.get("/admin/broker-accounts", headers=self._hdr).json()
        listed = {a["broker_account_id"]: a for a in listing}
        assert listed[new_ref]["is_active"] is False
        from investor.main import app
        assert new_ref not in app.state.adapters

    def test_delete_nonexistent_404(self, client):
        resp = client.request("DELETE", "/admin/broker-accounts/999", headers=self._hdr)
        assert resp.status_code == 404
