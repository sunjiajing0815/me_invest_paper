"""Tests for the warn-only large-target-edit notice (P2.2)."""
from __future__ import annotations

import json
from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing, session_scope
from investor.main import _warn_large_target_edit
from investor.models import Base, TargetChangeEvent
from investor.services.accounts import AccountInfo
from investor.services.email import FakeEmailer


@pytest.fixture()
def _engine() -> Generator[None, None, None]:
    eng = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(eng)
    override_engine_for_testing(eng)
    yield
    eng.dispose()


def _event(max_shift_pp: float, old: dict, new: dict) -> None:
    with session_scope() as s:
        s.add(TargetChangeEvent(
            broker_account_id=1, ts=datetime.now(UTC), source="admin_endpoint",
            diff_json=json.dumps({"old": old, "new": new}), max_shift_pp=max_shift_pp,
            created_at=datetime.now(UTC),
        ))


def _settings() -> MagicMock:
    return MagicMock(target_edit_warn_threshold_pct=10.0, email_to="x@y.com")


_ACCT = AccountInfo(account_ref=1, nickname="Alpaca", broker="alpaca")


def test_warn_emails_on_large_shift(_engine: None) -> None:
    _event(12.0, {"VOO": 30.0}, {"VOO": 42.0})
    emailer = FakeEmailer()
    _warn_large_target_edit(emailer, _settings(), _ACCT)
    assert len(emailer.sent) == 1
    msg = emailer.sent[0]
    assert "Large target edit" in msg["subject"]
    assert "VOO" in msg["html"] and "+12.0pp" in msg["html"]


def test_no_warn_below_threshold(_engine: None) -> None:
    _event(5.0, {"VOO": 30.0}, {"VOO": 35.0})
    emailer = FakeEmailer()
    _warn_large_target_edit(emailer, _settings(), _ACCT)
    assert emailer.sent == []


def test_no_event_no_email(_engine: None) -> None:
    emailer = FakeEmailer()
    _warn_large_target_edit(emailer, _settings(), _ACCT)
    assert emailer.sent == []
