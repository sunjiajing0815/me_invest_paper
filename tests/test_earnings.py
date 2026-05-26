"""Tests for src/investor/services/earnings.py."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from investor.services.earnings import (
    FakeEarningsClient,
    FinnhubEarningsClient,
    make_earnings_client,
)


def test_fake_records_calls() -> None:
    """FakeEarningsClient records the call arguments in calls list."""
    client = FakeEarningsClient(_canned={"AAPL": date(2026, 6, 1)})
    client.upcoming_earnings(["AAPL", "MSFT"], start=date(2026, 5, 26), end=date(2026, 6, 7))

    assert len(client.calls) == 1
    assert client.calls[0] == (["AAPL", "MSFT"], date(2026, 5, 26), date(2026, 6, 7))


def test_fake_returns_canned_filtered_to_window() -> None:
    """FakeEarningsClient filters canned results to [start, end] window."""
    client = FakeEarningsClient(
        _canned={"AAPL": date(2026, 6, 1), "MSFT": date(2026, 7, 15)}
    )
    result = client.upcoming_earnings(
        ["AAPL", "MSFT"], start=date(2026, 5, 26), end=date(2026, 6, 7)
    )

    assert result == {"AAPL": date(2026, 6, 1)}


def test_make_factory_no_key_returns_fake() -> None:
    """make_earnings_client returns FakeEarningsClient when finnhub_api_key is empty."""
    settings = MagicMock()
    settings.finnhub_api_key = ""

    result = make_earnings_client(settings)

    assert isinstance(result, FakeEarningsClient)


def test_make_factory_with_key_returns_concrete() -> None:
    """make_earnings_client returns FinnhubEarningsClient when finnhub_api_key is set."""
    settings = MagicMock()
    settings.finnhub_api_key = "test-key"

    result = make_earnings_client(settings)

    assert isinstance(result, FinnhubEarningsClient)
    assert result._api_key == "test-key"


def test_sdk_exception_returns_empty() -> None:
    """FinnhubEarningsClient returns {} when the underlying SDK raises an exception."""
    client = FinnhubEarningsClient(api_key="k")
    mock_finnhub = MagicMock()
    mock_finnhub.Client.return_value.earnings_calendar.side_effect = Exception("api error")
    with patch.dict("sys.modules", {"finnhub": mock_finnhub}):
        result = client.upcoming_earnings(
            ["AAPL"], start=date(2026, 5, 26), end=date(2026, 6, 2)
        )
    assert result == {}
