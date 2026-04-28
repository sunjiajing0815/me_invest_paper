"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear broker-related env vars so each test sets only what it needs."""
    for key in [
        "BROKER", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
        "ALPACA_BASE_URL", "SQLITE_PATH", "TARGETS_PATH",
    ]:
        monkeypatch.delenv(key, raising=False)
