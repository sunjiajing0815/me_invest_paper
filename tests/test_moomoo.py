"""Tests for brokers/moomoo.py.

All tests mock the futu library (OpenD not required).
The adapter is always instantiated with paper=True to avoid real broker calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from investor.brokers.moomoo import MoomooAdapter, _strip_market_prefix

# ── _strip_market_prefix ──────────────────────────────────────────────────────

def test_strip_us_prefix() -> None:
    assert _strip_market_prefix("US.AAPL") == "AAPL"


def test_strip_hk_prefix() -> None:
    assert _strip_market_prefix("HK.0700") == "0700"


def test_strip_no_prefix_passthrough() -> None:
    assert _strip_market_prefix("AAPL") == "AAPL"


def test_strip_multiple_dots_only_first_removed() -> None:
    assert _strip_market_prefix("US.AAPL.X") == "AAPL.X"


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def ft_mock() -> MagicMock:
    """Patch futu so MoomooAdapter.__init__ works without OpenD."""
    ft = MagicMock()
    ft.RET_OK = 0
    ft.TrdEnv.SIMULATE = "SIMULATE"
    ft.TrdEnv.REAL = "REAL"
    ft.TrdSide.BUY = "BUY"
    ft.TrdSide.SELL = "SELL"
    ft.OrderType.NORMAL = "NORMAL"
    ft.ModifyOrderOp.CANCEL = "CANCEL"
    ft.SecurityFirm.FUTUSECURITIES = "FUTUSECURITIES"
    ft.OpenSecTradeContext.return_value = MagicMock()
    ft.OpenQuoteContext.return_value = MagicMock()
    with patch.dict("sys.modules", {"futu": ft}):
        yield ft


@pytest.fixture()
def adapter(ft_mock: MagicMock) -> MoomooAdapter:
    return MoomooAdapter(host="localhost", port=11111, paper=True)


# ── encryption (RSA) ──────────────────────────────────────────────────────────

def test_encryption_enabled_when_rsa_key_path_given(ft_mock: MagicMock) -> None:
    """OpenD with encryption on: the SDK must enable proto encryption + load the key."""
    MoomooAdapter(host="h", port=11111, paper=True, rsa_key_path="/app/data/secrets/k.txt")
    ft_mock.SysConfig.enable_proto_encrypt.assert_called_once_with(True)
    ft_mock.SysConfig.set_init_rsa_file.assert_called_once_with("/app/data/secrets/k.txt")


def test_no_encryption_when_rsa_key_path_absent(ft_mock: MagicMock) -> None:
    """Default (no key) stays unencrypted — Alpaca-only setups are unaffected."""
    MoomooAdapter(host="h", port=11111, paper=True)
    ft_mock.SysConfig.enable_proto_encrypt.assert_not_called()
    ft_mock.SysConfig.set_init_rsa_file.assert_not_called()


# ── account base currency ─────────────────────────────────────────────────────

def test_get_account_requests_configured_currency(ft_mock: MagicMock) -> None:
    """accinfo_query must be pinned to the account's base currency — Futu defaults to
    HKD, which silently converts an AUD/USD account's totals."""
    ft_mock.Currency.USD = "USD"
    ft_mock.Currency.AUD = "AUD"
    tc = ft_mock.OpenSecTradeContext.return_value
    tc.accinfo_query.return_value = (
        ft_mock.RET_OK,
        pd.DataFrame([{"acc_id": "1819", "cash": 1000.0, "total_assets": 5000.0, "power": 2000.0}]),
    )
    adapter = MoomooAdapter(host="h", port=11111, paper=False, currency="USD")
    acct = adapter.get_account()
    assert tc.accinfo_query.call_args.kwargs["currency"] == "USD"
    assert acct.equity_usd == 5000.0
    assert acct.cash_usd == 1000.0


def test_get_account_currency_defaults_to_usd(ft_mock: MagicMock) -> None:
    ft_mock.Currency.USD = "USD"
    tc = ft_mock.OpenSecTradeContext.return_value
    tc.accinfo_query.return_value = (
        ft_mock.RET_OK,
        pd.DataFrame([{"acc_id": "x", "cash": 0.0, "total_assets": 0.0, "power": 0.0}]),
    )
    MoomooAdapter(host="h", port=11111, paper=False).get_account()
    assert tc.accinfo_query.call_args.kwargs["currency"] == "USD"


# ── get_positions ─────────────────────────────────────────────────────────────

def test_get_positions_strips_prefix_and_maps_fields(
    adapter: MoomooAdapter,
) -> None:
    df = pd.DataFrame([{
        "code": "US.AAPL",
        "qty": 10.0,
        "cost_price": 150.0,
        "market_val": 1600.0,
    }])
    adapter._trade_ctx.position_list_query.return_value = (0, df)

    positions = adapter.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.ticker == "AAPL"
    assert pos.qty == pytest.approx(10.0)
    assert pos.avg_cost == pytest.approx(150.0)
    assert pos.market_value == pytest.approx(1600.0)


def test_get_positions_empty_returns_empty_list(adapter: MoomooAdapter) -> None:
    df = pd.DataFrame([], columns=["code", "qty", "cost_price", "market_val"])
    adapter._trade_ctx.position_list_query.return_value = (0, df)
    assert adapter.get_positions() == []


def test_get_positions_labels_native_currency_from_market(adapter: MoomooAdapter) -> None:
    """Per-position currency is derived from the market prefix: US.→USD, AU.→AUD."""
    df = pd.DataFrame([
        {"code": "US.AAPL", "qty": 10.0, "cost_price": 150.0, "market_val": 1600.0},
        {"code": "AU.CSL", "qty": 25.0, "cost_price": 130.0, "market_val": 2415.0},
    ])
    adapter._trade_ctx.position_list_query.return_value = (0, df)
    by_ticker = {p.ticker: p for p in adapter.get_positions()}
    assert by_ticker["AAPL"].currency == "USD"
    assert by_ticker["CSL"].currency == "AUD"


# ── get_activities ────────────────────────────────────────────────────────────

def test_get_activities_uses_deal_list_query_not_order_list(
    adapter: MoomooAdapter,
) -> None:
    df = pd.DataFrame(
        [], columns=["code", "qty", "price", "trd_side", "order_id", "create_time", "remark"]
    )
    adapter._trade_ctx.deal_list_query.return_value = (0, df)
    adapter.get_activities(datetime(2026, 1, 1, tzinfo=UTC))
    adapter._trade_ctx.deal_list_query.assert_called_once()
    adapter._trade_ctx.order_list_query.assert_not_called()


def test_get_activities_maps_remark_to_client_order_id(adapter: MoomooAdapter) -> None:
    df = pd.DataFrame([{
        "code": "US.AAPL",
        "qty": 5.0,
        "price": 100.0,
        "trd_side": "BUY",
        "order_id": "ord-42",
        "create_time": "2026-05-01 10:00:00",
        "remark": "sug-7",
    }])
    adapter._trade_ctx.deal_list_query.return_value = (0, df)
    activities = adapter.get_activities(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(activities) == 1
    assert activities[0].client_order_id == "sug-7"
    assert activities[0].ticker == "AAPL"
    assert activities[0].side == "buy"


def test_get_activities_empty_remark_gives_none_client_order_id(
    adapter: MoomooAdapter,
) -> None:
    df = pd.DataFrame([{
        "code": "US.MSFT",
        "qty": 3.0,
        "price": 200.0,
        "trd_side": "BUY",
        "order_id": "ord-43",
        "create_time": "2026-05-01 11:00:00",
        "remark": "",
    }])
    adapter._trade_ctx.deal_list_query.return_value = (0, df)
    activities = adapter.get_activities(datetime(2026, 1, 1, tzinfo=UTC))
    assert len(activities) == 1
    assert activities[0].client_order_id is None


# ── get_bars ──────────────────────────────────────────────────────────────────

def test_get_bars_raises_not_implemented(adapter: MoomooAdapter) -> None:
    """MoomooAdapter must never serve bar data — bars always come from Alpaca."""
    with pytest.raises(NotImplementedError):
        adapter.get_bars("AAPL", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC))


# ── submit_order ──────────────────────────────────────────────────────────────

def test_submit_order_adds_us_prefix_and_returns_confirmation(
    adapter: MoomooAdapter, ft_mock: MagicMock
) -> None:
    from investor.brokers.base import OrderRequest

    df = pd.DataFrame([{"order_id": "moomoo-456", "order_status": "submitted"}])
    adapter._trade_ctx.place_order.return_value = (0, df)

    req = OrderRequest(
        client_order_id="sug-7",
        ticker="AAPL",
        side="buy",
        qty=5.0,
        limit_price=150.0,
    )
    conf = adapter.submit_order(req)
    assert conf.broker_order_id == "moomoo-456"
    assert conf.client_order_id == "sug-7"

    call_kwargs = adapter._trade_ctx.place_order.call_args
    assert call_kwargs.kwargs.get("code") == "US.AAPL" or (
        len(call_kwargs.args) > 0 and "US.AAPL" in str(call_kwargs)
    )
