"""The paper-only invariant: this build must never reach a live account.

Four independent layers are asserted here — see docs/adr/0036-paper-only-public-build.md.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from investor.brokers import make_account_adapter
from investor.brokers.alpaca import AlpacaAdapter
from investor.config import Settings
from investor.safety import PAPER_ONLY, LiveTradingBlocked, assert_paper_flag, assert_paper_only


def _settings_ns() -> SimpleNamespace:
    return SimpleNamespace(alpaca_api_key="k", alpaca_secret_key="s")


def test_paper_only_flag_is_on() -> None:
    assert PAPER_ONLY is True


# ── L1: config ────────────────────────────────────────────────────────────────

def test_broker_alpaca_live_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER", "alpaca_live")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    with pytest.raises(Exception, match="broker must be one of"):
        Settings()


def test_broker_moomoo_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER", "moomoo")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    with pytest.raises(Exception, match="broker must be one of"):
        Settings()


def test_broker_alpaca_paper_still_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER", "alpaca_paper")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert Settings().broker == "alpaca_paper"


# ── L0: adapter constructor ───────────────────────────────────────────────────

def test_alpaca_adapter_refuses_live() -> None:
    with patch("investor.brokers.alpaca.TradingClient"), pytest.raises(LiveTradingBlocked):
        AlpacaAdapter("k", "s", paper=False)


def test_alpaca_adapter_paper_exposes_paper_attribute() -> None:
    with patch("investor.brokers.alpaca.TradingClient"):
        adapter = AlpacaAdapter("k", "s", paper=True)
    assert adapter.paper is True


# ── L2: factory ignores a live connection_config ──────────────────────────────

def test_make_account_adapter_ignores_paper_false() -> None:
    with patch("investor.brokers.alpaca.TradingClient") as mock_tc:
        adapter = make_account_adapter(
            broker="alpaca", connection_config={"paper": False}, settings=_settings_ns()
        )
    assert mock_tc.call_args.kwargs["paper"] is True
    assert adapter.paper is True


def test_make_account_adapter_moomoo_is_gone() -> None:
    with pytest.raises(NotImplementedError):
        make_account_adapter(broker="moomoo", connection_config={}, settings=_settings_ns())


# ── assert helpers ────────────────────────────────────────────────────────────

def test_assert_paper_flag_raises_on_false() -> None:
    with pytest.raises(LiveTradingBlocked):
        assert_paper_flag(False, source="test")


def test_assert_paper_flag_passes_on_true() -> None:
    assert assert_paper_flag(True, source="test") is None


def test_assert_paper_only_raises_on_live_adapter() -> None:
    with pytest.raises(LiveTradingBlocked):
        assert_paper_only(SimpleNamespace(paper=False))


def test_assert_paper_only_raises_when_attribute_missing() -> None:
    """An adapter that cannot prove it is paper-mode is refused, not trusted."""
    with pytest.raises(LiveTradingBlocked):
        assert_paper_only(SimpleNamespace())


def test_assert_paper_only_passes_on_paper_adapter() -> None:
    assert assert_paper_only(SimpleNamespace(paper=True)) is None
