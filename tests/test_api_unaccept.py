"""Endpoint tests for the un-accept routes — called as plain functions with a fake Request
(avoids the app lifespan, which does real broker/DB I/O)."""
from __future__ import annotations

from collections.abc import Generator
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from investor.db import override_engine_for_testing, session_scope
from investor.main import admin_unaccept, unaccept_act, unaccept_confirm
from investor.models import Base, OrderExecution, OrderSuggestion
from investor.services.magic_link import sign_action

SECRET = "test-secret-xxxxxxxxxxxxxxxxxxxxxx"


@pytest.fixture()
def engine() -> Generator[Engine, None, None]:
    eng = create_engine("sqlite:///:memory:", poolclass=StaticPool, future=True)
    Base.metadata.create_all(eng)
    override_engine_for_testing(eng)
    yield eng
    eng.dispose()


def _seed(adapter_status: str = "new") -> tuple[int, MagicMock]:
    adapter = MagicMock()
    adapter.get_order.return_value = MagicMock(status=adapter_status, broker_order_id="bo-1")
    with session_scope() as s:
        sug = OrderSuggestion(broker_account_id=61, week_of=date(2026, 6, 8), ticker="VOO",
                              side="buy", qty=5.0, limit_price=400.0, reason="t", status="accepted")
        s.add(sug)
        s.flush()
        s.add(OrderExecution(broker_account_id=61, suggestion_id=sug.id, ticker="VOO", side="buy",
                             filled_qty=0.0, broker="alpaca", broker_order_id="bo-1", dry_run=False,
                             status="accepted_for_routing", match_method="auto_trade_placed"))
        sid = sug.id
    return sid, adapter


def _req(adapter: Any) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(magic_link_secret=SECRET),
        adapters={61: adapter},
    )))


def test_get_confirm_has_no_side_effect(engine: Engine) -> None:
    sid, adapter = _seed()
    tok = sign_action(sid, "unaccept", SECRET)
    resp = unaccept_confirm(sid, tok, _req(adapter))
    assert resp.status_code == 200
    assert "Confirm" in resp.body.decode()
    adapter.cancel_order.assert_not_called()
    with session_scope() as s:
        assert s.get(OrderSuggestion, sid).status == "accepted"  # GET changes nothing


def test_post_unaccept_cancels(engine: Engine) -> None:
    sid, adapter = _seed()
    tok = sign_action(sid, "unaccept", SECRET)
    resp = unaccept_act(sid, tok, _req(adapter))
    assert resp.status_code == 200
    adapter.cancel_order.assert_called_once_with("bo-1")
    with session_scope() as s:
        assert s.get(OrderSuggestion, sid).status == "cancelled"


def test_bad_token_rejected(engine: Engine) -> None:
    sid, adapter = _seed()
    with pytest.raises(HTTPException) as ei:
        unaccept_confirm(sid, "bad.token", _req(adapter))
    assert ei.value.status_code == 400


def test_admin_unaccept(engine: Engine) -> None:
    sid, adapter = _seed()
    out = admin_unaccept(sid, _req(adapter))
    assert out["result"] == "cancelled"
    with session_scope() as s:
        assert s.get(OrderSuggestion, sid).status == "cancelled"


def test_get_unaccept_url_resolves_to_confirm_not_generic_action() -> None:
    """Regression: GET /suggestions/{sid}/unaccept must route to unaccept_confirm, not the
    generic /suggestions/{sid}/{action} handler (which rejects 'unaccept' as invalid action).
    FastAPI matches in registration order, so the specific route must come first."""
    from starlette.routing import Match

    from investor.main import app

    scope = {"type": "http", "method": "GET", "path": "/suggestions/5/unaccept"}
    matched = next(
        (r for r in app.router.routes if r.matches(scope)[0] == Match.FULL), None
    )
    assert matched is not None
    assert matched.endpoint.__name__ == "unaccept_confirm"

    # The generic route still wins for real accept/reject actions.
    for action in ("accept", "reject"):
        s2 = {"type": "http", "method": "GET", "path": f"/suggestions/5/{action}"}
        m = next((r for r in app.router.routes if r.matches(s2)[0] == Match.FULL), None)
        assert m is not None and m.endpoint.__name__ == "suggestion_magic_link"
