# Un-accept path + daily order status — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user see working/committed orders in the daily email and un-accept (cancel + revert) an accepted suggestion safely.

**Architecture:** A shared `cancel_working_execution` helper (re-queries the broker, handles working/partial/filled/terminal) is reused by a thin `unaccept_suggestion` service and by the existing expiry sweep. Un-accept is exposed as a prefetch-safe two-step magic-link (GET confirm page → POST acts) plus an admin endpoint, and sets a new terminal suggestion status `cancelled` (which auto-trade ignores, closing the re-place footgun). The daily email gains an "Open & committed orders" section with un-accept links.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Jinja2, pytest. Design spec: `plans/unaccept_path_design.md`.

---

## File structure

- Create `src/investor/services/orders.py` — `CancelOutcome` enum + `cancel_working_execution()`.
- Create `src/investor/services/unaccept.py` — `UnacceptResult` enum + `unaccept_suggestion()`.
- Modify `src/investor/jobs/suggestion_expiry.py` — refactor inline cancel onto the helper.
- Modify `src/investor/services/daily_report.py` — `CommittedOrderRow` + gather in `compose_daily_report`.
- Modify `src/investor/jobs/daily_report.py` — pass `base_url` + per-row signed `unaccept` tokens.
- Modify `templates/daily_report.html.j2` + `templates/daily_report.txt.j2` — "Open & committed orders" section.
- Create `templates/unaccept_confirm.html.j2` + `templates/unaccept_result.html.j2` — confirm page + outcome page.
- Modify `src/investor/main.py` — `GET/POST /suggestions/{sid}/unaccept`, `POST /admin/suggestions/{sid}/unaccept`.
- Tests: `tests/test_orders.py`, `tests/test_unaccept.py`, `tests/test_daily_report_committed.py`, plus additions to `tests/test_suggestion_expiry.py` and the API test module.

Reference facts (verified):
- `OrderConfirmation` (from `adapter.get_order`) = `(broker_order_id, client_order_id, status, submitted_at)` — **no `filled_qty`**; partial qty is recorded later by reconciliation.
- Broker status strings: `filled`, `partially_filled`, terminal `{canceled, expired, rejected, done_for_day, replaced}`, else working (`new`/`accepted`/`pending_new`/`held`).
- `OrderExecution` fields: `id, broker_account_id, suggestion_id, ticker, side, submitted_qty, filled_qty, limit_price, filled_price, broker_order_id, client_order_id, dry_run, status, created_at`.
- `OrderSuggestion` fields: `id, broker_account_id, week_of, ticker, side, qty, limit_price, status, acted_at`.
- `sign_action(sid, action, secret, ttl=…)` / `verify_action(sid, action, token, secret)` — `action` is a free string; use `"unaccept"`.
- Endpoints resolve adapters via `request.app.state.adapters[account_ref]`; `_require_account(sid)` and `admin_auth` exist in `main.py`; `settings.app_base_url`, `settings.magic_link_secret`.

---

## Task 1: `cancel_working_execution` helper

**Files:**
- Create: `src/investor/services/orders.py`
- Test: `tests/test_orders.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_orders.py
from __future__ import annotations
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from investor.services.orders import CancelOutcome, cancel_working_execution


def _exe(status="accepted_for_routing", boid="bo-1"):
    return SimpleNamespace(broker_order_id=boid, status=status)


def _conf(status):
    return SimpleNamespace(broker_order_id="bo-1", client_order_id="sug-1",
                           status=status, submitted_at=datetime.now(UTC))


def test_working_order_cancelled():
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("new")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.CANCELLED
    adapter.cancel_order.assert_called_once_with("bo-1")
    assert exe.status == "broker_cancelled"


def test_filled_order_not_cancelled():
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("filled")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.ALREADY_FILLED
    adapter.cancel_order.assert_not_called()
    assert exe.status == "accepted_for_routing"  # unchanged


def test_partial_order_cancels_remainder_keeps_status():
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("partially_filled")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.PARTIAL
    adapter.cancel_order.assert_called_once_with("bo-1")
    assert exe.status == "accepted_for_routing"  # reconciliation will set fill


def test_already_terminal_is_noop():
    adapter = MagicMock()
    adapter.get_order.return_value = _conf("canceled")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.NOOP
    adapter.cancel_order.assert_not_called()
    assert exe.status == "broker_cancelled"


def test_get_order_raises_attempts_cancel_conservatively():
    adapter = MagicMock()
    adapter.get_order.side_effect = RuntimeError("broker down")
    exe = _exe()
    out = cancel_working_execution(adapter, exe)
    assert out is CancelOutcome.CANCELLED
    adapter.cancel_order.assert_called_once_with("bo-1")
    assert exe.status == "broker_cancelled"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_orders.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'investor.services.orders'`.

- [ ] **Step 3: Write the implementation**

```python
# src/investor/services/orders.py
"""Broker-order cancellation helper shared by the expiry sweep and un-accept.

Re-queries the broker before cancelling so a just-filled order is never clobbered.
OrderConfirmation has no filled_qty, so a partial fill is detected by status only —
the filled quantity is recorded later by reconciliation (get_activities)."""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from ..brokers.base import BrokerAdapter

log = logging.getLogger(__name__)

_TERMINAL = frozenset({"canceled", "expired", "rejected", "done_for_day", "replaced"})


class CancelOutcome(str, Enum):
    CANCELLED = "cancelled"        # was working; cancel sent; execution -> broker_cancelled
    PARTIAL = "partial"           # partially filled; remainder cancelled; fill stands
    ALREADY_FILLED = "already_filled"  # fully filled; nothing cancelled
    NOOP = "noop"                 # already terminal at the broker


def cancel_working_execution(adapter: BrokerAdapter, execution: Any) -> CancelOutcome:
    """Cancel a working broker order for *execution*; mutate execution.status in place.

    *execution* must have `.broker_order_id` and `.status`. Caller commits the session.
    """
    boid = execution.broker_order_id
    try:
        conf = adapter.get_order(boid)
    except Exception as exc:  # noqa: BLE001 — broker unreachable: cancel conservatively
        log.warning("cancel_working_execution: get_order(%s) failed: %s", boid, exc)
        _try_cancel(adapter, boid)
        execution.status = "broker_cancelled"
        return CancelOutcome.CANCELLED

    status = conf.status
    if status == "filled":
        return CancelOutcome.ALREADY_FILLED
    if status in _TERMINAL:
        execution.status = "broker_cancelled"
        return CancelOutcome.NOOP

    _try_cancel(adapter, boid)
    if status == "partially_filled":
        return CancelOutcome.PARTIAL  # leave status; reconciliation records the fill
    execution.status = "broker_cancelled"
    return CancelOutcome.CANCELLED


def _try_cancel(adapter: BrokerAdapter, broker_order_id: str) -> None:
    try:
        adapter.cancel_order(broker_order_id)
    except Exception as exc:  # noqa: BLE001 — already-terminal cancels can 422; log + proceed
        log.warning("cancel_working_execution: cancel_order(%s) failed: %s",
                    broker_order_id, exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orders.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/investor/services/orders.py tests/test_orders.py
uv run mypy src/
git add src/investor/services/orders.py tests/test_orders.py
git commit -m "feat(orders): shared cancel_working_execution helper"
```

---

## Task 2: Refactor the expiry sweep onto the helper

**Files:**
- Modify: `src/investor/jobs/suggestion_expiry.py:59-107` (the accepted-stale cancel block)
- Test: `tests/test_suggestion_expiry.py` (existing tests must still pass; add a partial test)

- [ ] **Step 1: Add a failing test for partial-fill handling during the sweep**

```python
# tests/test_suggestion_expiry.py — add to the existing module
def test_sweep_partial_fill_cancels_remainder(monkeypatch):
    """An accepted, expired suggestion whose order partially filled: remainder cancelled,
    suggestion still expired, execution left for reconciliation (not forced cancelled)."""
    # Build via the module's existing fixtures/session helper; mock adapter:
    #   adapter.get_order(...).status == "partially_filled"
    # After sweep: suggestion.status == "expired"; cancel_order called once.
    # (Mirror the existing accepted-stale test setup in this file.)
    ...
```

> Implementation note for the worker: copy the setup of the existing "accepted stale cancels order" test in this file; set the mocked `adapter.get_order` to return `status="partially_filled"`, assert `adapter.cancel_order` was called and the suggestion ends `expired`.

- [ ] **Step 2: Run the existing suite to confirm current behavior**

Run: `uv run pytest tests/test_suggestion_expiry.py -q`
Expected: existing tests PASS; the new partial test FAILS (current code has no partial branch).

- [ ] **Step 3: Refactor the sweep to call the helper**

Replace the inline cancel/verify block (the `if adapter is not None:` body that calls `cancel_order` + `get_order`) with:

```python
from ..services.orders import CancelOutcome, cancel_working_execution
# ...
for sug in accepted_stale:
    if adapter is not None:
        exec_row = s.scalars(
            select(OrderExecution).where(
                OrderExecution.suggestion_id == sug.id,
                OrderExecution.dry_run.is_(False),
                OrderExecution.broker_order_id.is_not(None),
            )
        ).first()
        if exec_row is not None:
            outcome = cancel_working_execution(adapter, exec_row)
            if outcome in (CancelOutcome.CANCELLED, CancelOutcome.PARTIAL):
                cancelled += 1
            elif outcome is CancelOutcome.ALREADY_FILLED:
                log.warning("sweep: sug-%d order %s filled before cancel — leaving for "
                            "reconciliation", sug.id, exec_row.broker_order_id)
    sug.status = "expired"
    sug.acted_at = now
```

- [ ] **Step 4: Run tests to verify all pass**

Run: `uv run pytest tests/test_suggestion_expiry.py -q`
Expected: PASS (existing + new partial test).

- [ ] **Step 5: Commit**

```bash
git add src/investor/jobs/suggestion_expiry.py tests/test_suggestion_expiry.py
git commit -m "refactor(expiry): use shared cancel_working_execution (adds partial handling)"
```

---

## Task 3: `unaccept_suggestion` service

**Files:**
- Create: `src/investor/services/unaccept.py`
- Test: `tests/test_unaccept.py`

- [ ] **Step 1: Write the failing tests** (use the in-memory DB harness, mirroring `tests/test_auto_trade.py` session fixtures)

```python
# tests/test_unaccept.py
from __future__ import annotations
from datetime import UTC, date, datetime
from unittest.mock import MagicMock
import pytest
from investor.db import session_scope
from investor.models import OrderExecution, OrderSuggestion
from investor.services.unaccept import UnacceptResult, unaccept_suggestion

# Assume a module/session fixture initialises an in-memory DB (see conftest / test_auto_trade).

def _add_suggestion(s, status="accepted", week_of=None):
    sug = OrderSuggestion(broker_account_id=61, week_of=week_of or date(2026, 6, 8),
                          ticker="VOO", side="buy", qty=5, limit_price=400.0,
                          reason="t", status=status)
    s.add(sug); s.flush(); return sug

def _add_exec(s, sid, status="accepted_for_routing", boid="bo-1"):
    e = OrderExecution(broker_account_id=61, suggestion_id=sid, ticker="VOO", side="buy",
                       filled_qty=0.0, broker="alpaca", broker_order_id=boid,
                       client_order_id=f"sug-{sid}", dry_run=False, status=status,
                       match_method="auto_trade_placed")
    s.add(e); s.flush(); return e


def test_unaccept_working_order_cancels_and_marks_cancelled():
    adapter = MagicMock()
    adapter.get_order.return_value = MagicMock(status="new", broker_order_id="bo-1")
    with session_scope() as s:
        sug = _add_suggestion(s); _add_exec(s, sug.id)
        res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=61)
        assert res is UnacceptResult.CANCELLED
        assert s.get(OrderSuggestion, sug.id).status == "cancelled"
        adapter.cancel_order.assert_called_once_with("bo-1")


def test_unaccept_unplaced_suggestion_just_marks_cancelled():
    adapter = MagicMock()
    with session_scope() as s:
        sug = _add_suggestion(s)  # no execution
        res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=61)
        assert res is UnacceptResult.CANCELLED
        assert s.get(OrderSuggestion, sug.id).status == "cancelled"
        adapter.get_order.assert_not_called()


def test_unaccept_filled_refuses_and_leaves_status():
    adapter = MagicMock()
    adapter.get_order.return_value = MagicMock(status="filled", broker_order_id="bo-1")
    with session_scope() as s:
        sug = _add_suggestion(s); _add_exec(s, sug.id)
        res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=61)
        assert res is UnacceptResult.FILLED
        assert s.get(OrderSuggestion, sug.id).status == "accepted"  # unchanged


def test_unaccept_non_accepted_is_not_actionable():
    adapter = MagicMock()
    with session_scope() as s:
        sug = _add_suggestion(s, status="pending")
        res = unaccept_suggestion(s, adapter, sug.id, broker_account_id=61)
        assert res is UnacceptResult.NOT_ACTIONABLE
        assert s.get(OrderSuggestion, sug.id).status == "pending"


def test_unaccept_missing_suggestion():
    adapter = MagicMock()
    with session_scope() as s:
        res = unaccept_suggestion(s, adapter, 99999, broker_account_id=61)
        assert res is UnacceptResult.NOT_FOUND
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_unaccept.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'investor.services.unaccept'`.

- [ ] **Step 3: Write the implementation**

```python
# src/investor/services/unaccept.py
"""Un-accept an accepted suggestion: cancel any working broker order and mark it cancelled.

Reused by the magic-link confirm POST and the admin endpoint. Caller commits the session."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..brokers.base import BrokerAdapter
from ..models import OrderExecution, OrderSuggestion
from .orders import CancelOutcome, cancel_working_execution

log = logging.getLogger(__name__)


class UnacceptResult(str, Enum):
    CANCELLED = "cancelled"          # suggestion -> cancelled (order cancelled if any)
    PARTIAL = "partial"              # remainder cancelled, partial fill kept; -> cancelled
    FILLED = "filled"                # order fully filled; refused, suggestion unchanged
    NOT_ACTIONABLE = "not_actionable"  # suggestion not in 'accepted' state
    NOT_FOUND = "not_found"


def unaccept_suggestion(
    session: Session, adapter: BrokerAdapter, suggestion_id: int, *, broker_account_id: int
) -> UnacceptResult:
    sug = session.get(OrderSuggestion, suggestion_id)
    if sug is None or sug.broker_account_id != broker_account_id:
        return UnacceptResult.NOT_FOUND
    if sug.status != "accepted":
        return UnacceptResult.NOT_ACTIONABLE

    exe = session.scalars(
        select(OrderExecution)
        .where(
            OrderExecution.suggestion_id == sug.id,
            OrderExecution.broker_account_id == broker_account_id,
            OrderExecution.dry_run.is_(False),
            OrderExecution.broker_order_id.is_not(None),
        )
        .order_by(OrderExecution.created_at.desc())
    ).first()

    result = UnacceptResult.CANCELLED
    if exe is not None:
        outcome = cancel_working_execution(adapter, exe)
        if outcome is CancelOutcome.ALREADY_FILLED:
            return UnacceptResult.FILLED  # leave suggestion as-is
        if outcome is CancelOutcome.PARTIAL:
            result = UnacceptResult.PARTIAL

    sug.status = "cancelled"
    sug.acted_at = datetime.now(UTC)
    log.info("unaccept: sug-%d (%s) -> cancelled (%s)", sug.id, sug.ticker, result.value)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_unaccept.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/investor/services/unaccept.py tests/test_unaccept.py
uv run mypy src/
git add src/investor/services/unaccept.py tests/test_unaccept.py
git commit -m "feat(unaccept): unaccept_suggestion service"
```

---

## Task 4: Committed-order rows in `compose_daily_report`

**Files:**
- Modify: `src/investor/services/daily_report.py` (add `CommittedOrderRow`, gather + return it)
- Test: `tests/test_daily_report_committed.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daily_report_committed.py
from __future__ import annotations
from datetime import date
from investor.db import session_scope
from investor.models import OrderExecution, OrderSuggestion
from investor.services.daily_report import compose_daily_report
# (use the in-memory DB harness; seed a BrokerAccount/positions as other daily tests do)

def _monday(d): return d - __import__("datetime").timedelta(days=d.weekday())

def test_committed_rows_classify_status():
    with session_scope() as s:
        wk = _monday(date(2026, 6, 8))
        a = OrderSuggestion(broker_account_id=61, week_of=wk, ticker="VOO", side="buy",
                            qty=5, limit_price=400.0, reason="t", status="accepted")
        b = OrderSuggestion(broker_account_id=61, week_of=wk, ticker="QQQ", side="buy",
                            qty=2, limit_price=650.0, reason="t", status="accepted")
        s.add_all([a, b]); s.flush()
        s.add(OrderExecution(broker_account_id=61, suggestion_id=a.id, ticker="VOO",
                             side="buy", filled_qty=0.0, broker="alpaca",
                             broker_order_id="bo-1", dry_run=False,
                             status="accepted_for_routing", match_method="auto_trade_placed"))
        s.flush()
        report = compose_daily_report(s, broker_account_id=61)
    by = {r.ticker: r for r in report.committed_orders}
    assert by["VOO"].status_label == "Working" and by["VOO"].cancellable is True
    assert by["QQQ"].status_label == "Awaiting placement" and by["QQQ"].cancellable is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_daily_report_committed.py -q`
Expected: FAIL — `DailyReport` has no attribute `committed_orders`.

- [ ] **Step 3: Implement**

Add the dataclass + helper near the top of `services/daily_report.py`:

```python
@dataclass(frozen=True)
class CommittedOrderRow:
    sid: int
    ticker: str
    side: str
    qty: float
    limit_price: float
    status_label: str          # Working | Partially filled | Filled | Awaiting placement
    filled_price: float | None
    cancellable: bool


def _committed_status(exe: Any | None) -> tuple[str, bool]:
    if exe is None:
        return "Awaiting placement", True
    st = exe.status
    if st == "accepted_for_routing":
        return "Working", True
    if st == "partially_filled":
        return "Partially filled", True
    if st == "filled":
        return "Filled", False
    if st == "broker_cancelled":
        return "Order cancelled", True
    return st, False
```

Add `committed_orders: list[CommittedOrderRow] = field(default_factory=list)` to `DailyReport`.

In `compose_daily_report`, before the `return`, gather the rows (this-week accepted suggestions + latest real execution):

```python
    from datetime import timedelta
    from sqlalchemy import select
    from ..models import OrderExecution, OrderSuggestion

    week_monday = today - timedelta(days=today.weekday())
    accepted_sugs = session.scalars(
        select(OrderSuggestion).where(
            OrderSuggestion.broker_account_id == broker_account_id,
            OrderSuggestion.status == "accepted",
            OrderSuggestion.week_of == week_monday,
        ).order_by(OrderSuggestion.ticker)
    ).all()
    committed: list[CommittedOrderRow] = []
    for sug in accepted_sugs:
        exe = session.scalars(
            select(OrderExecution).where(
                OrderExecution.suggestion_id == sug.id,
                OrderExecution.dry_run.is_(False),
            ).order_by(OrderExecution.created_at.desc())
        ).first()
        label, cancellable = _committed_status(exe)
        committed.append(CommittedOrderRow(
            sid=sug.id, ticker=sug.ticker, side=sug.side, qty=sug.qty,
            limit_price=sug.limit_price, status_label=label,
            filled_price=(exe.filled_price if exe else None), cancellable=cancellable,
        ))
```

Add `committed_orders=committed` to the `DailyReport(...)` return.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_daily_report_committed.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/investor/services/daily_report.py tests/test_daily_report_committed.py
uv run mypy src/
git add src/investor/services/daily_report.py tests/test_daily_report_committed.py
git commit -m "feat(daily-report): committed-order rows (accepted suggestions + execution status)"
```

---

## Task 5: Daily-email "Open & committed orders" section + token wiring

**Files:**
- Modify: `templates/daily_report.html.j2` (new section after the header/untracked, before Allocation)
- Modify: `templates/daily_report.txt.j2` (text equivalent)
- Modify: `src/investor/jobs/daily_report.py:76-83` (pass `base_url` + `unaccept_tokens`)
- Test: extend `tests/test_email_templates.py`

- [ ] **Step 1: Write the failing render test**

```python
# tests/test_email_templates.py — add
def test_daily_committed_orders_section_renders_unaccept_link():
    from investor.services.daily_report import CommittedOrderRow
    html = _render_daily(committed=[
        CommittedOrderRow(sid=7, ticker="VOO", side="buy", qty=5, limit_price=400.0,
                          status_label="Working", filled_price=None, cancellable=True),
        CommittedOrderRow(sid=8, ticker="QQQ", side="buy", qty=2, limit_price=650.0,
                          status_label="Filled", filled_price=651.0, cancellable=False),
    ])
    assert "Open &amp; Committed Orders" in html or "Committed" in html
    assert "/suggestions/7/unaccept?token=tok7" in html   # cancellable -> link
    assert "/suggestions/8/unaccept" not in html          # filled -> no link
```

> Worker note: extend the `_render_daily` helper in this file to accept `committed=None`,
> set `report.committed_orders = committed or []`, and pass
> `base_url="https://x", unaccept_tokens={7: "tok7", 8: "tok8"}` to `render_template`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_email_templates.py::test_daily_committed_orders_section_renders_unaccept_link -q`
Expected: FAIL (section/link absent).

- [ ] **Step 3: Add the template section**

In `templates/daily_report.html.j2`, after `{{ ui.untracked_box(...) }}` and before `{{ ui.section("Allocation") }}`:

```jinja
{% if report.committed_orders %}
{{ ui.section("Open & Committed Orders") }}
<div class="sscroll" style="overflow-x:auto; -webkit-overflow-scrolling:touch;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-family:Arial,Helvetica,sans-serif; font-size:13px; margin:0 0 8px 0;">
  <thead>
    <tr style="background:{{ ui.HEADBG }}; color:{{ ui.MUTED }};">
      <th style="padding:7px 10px; text-align:left; border-bottom:2px solid {{ ui.LINE2 }};">Ticker</th>
      <th style="padding:7px 10px; text-align:center; border-bottom:2px solid {{ ui.LINE2 }};">Side</th>
      <th style="padding:7px 10px; text-align:right; border-bottom:2px solid {{ ui.LINE2 }};">Qty</th>
      <th style="padding:7px 10px; text-align:right; border-bottom:2px solid {{ ui.LINE2 }};">Limit</th>
      <th style="padding:7px 10px; text-align:left; border-bottom:2px solid {{ ui.LINE2 }};">Status</th>
      <th style="padding:7px 10px; text-align:left; border-bottom:2px solid {{ ui.LINE2 }};">Action</th>
    </tr>
  </thead>
  <tbody>
    {% for r in report.committed_orders %}
    <tr style="border-bottom:1px solid {{ ui.LINE }}; color:{{ ui.BODY }};">
      <td style="padding:6px 10px; font-weight:bold;">{{ r.ticker }}</td>
      <td style="padding:6px 10px; text-align:center;">{{ r.side | upper }}</td>
      <td style="padding:6px 10px; text-align:right;">{{ "{:.0f}".format(r.qty) }}</td>
      <td style="padding:6px 10px; text-align:right;">${{ "{:,.2f}".format(r.limit_price) }}</td>
      <td style="padding:6px 10px;">{{ r.status_label }}{% if r.filled_price %} @ ${{ "{:,.2f}".format(r.filled_price) }}{% endif %}</td>
      <td style="padding:6px 10px;">{% if r.cancellable and base_url is defined and unaccept_tokens is defined %}<a href="{{ base_url }}/suggestions/{{ r.sid }}/unaccept?token={{ unaccept_tokens[r.sid] }}" style="color:{{ ui.NEG }};">Un-accept</a>{% else %}&mdash;{% endif %}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
</div>
<p style="font-family:Arial,Helvetica,sans-serif; font-size:11px; color:{{ ui.MUTED }}; margin:0 0 24px 0;">Un-accept opens a confirmation page; it cancels any working broker order and reverts the suggestion.</p>
{% endif %}
```

Add a plain-text block to `templates/daily_report.txt.j2` listing the same rows (ticker, side, qty, status_label; no link).

- [ ] **Step 4: Wire the job to pass tokens**

In `src/investor/jobs/daily_report.py`, replace the html render call (lines ~76-79):

```python
    from ..services.magic_link import sign_action
    unaccept_tokens = {
        r.sid: sign_action(r.sid, "unaccept", settings.magic_link_secret)
        for r in report.committed_orders if r.cancellable
    }
    html = render_template(
        "daily_report.html.j2", report=report, etf_tickers=etf_tickers,
        account_nickname=account.nickname, account_broker=account.broker,
        base_url=settings.app_base_url, unaccept_tokens=unaccept_tokens,
    )
```

- [ ] **Step 5: Run test + commit**

Run: `uv run pytest tests/test_email_templates.py -q`
Expected: PASS.

```bash
git add templates/daily_report.html.j2 templates/daily_report.txt.j2 src/investor/jobs/daily_report.py tests/test_email_templates.py
git commit -m "feat(daily-email): Open & Committed Orders section with un-accept links"
```

---

## Task 6: Endpoints — confirm page (GET) + un-accept (POST) + admin

**Files:**
- Create: `templates/unaccept_confirm.html.j2`, `templates/unaccept_result.html.j2`
- Modify: `src/investor/main.py` (3 routes)
- Test: the API test module (e.g. `tests/test_api_suggestions.py`; mirror the existing accept/reject magic-link test)

- [ ] **Step 1: Write failing endpoint tests**

```python
# tests/test_api_unaccept.py  (use FastAPI TestClient as the existing magic-link tests do)
# Assumes a fixture `client` with app.state.adapters[61] = MagicMock and a seeded
# accepted suggestion sid with an accepted_for_routing execution.

def test_get_confirm_page_has_no_side_effect(client, accepted_sid, secret):
    from investor.services.magic_link import sign_action
    tok = sign_action(accepted_sid, "unaccept", secret)
    r = client.get(f"/suggestions/{accepted_sid}/unaccept?token={tok}")
    assert r.status_code == 200 and "Confirm" in r.text
    # status unchanged by the GET
    # (assert via a DB read that suggestion is still 'accepted')

def test_post_unaccept_cancels(client, accepted_sid, secret, adapter_mock):
    from investor.services.magic_link import sign_action
    adapter_mock.get_order.return_value = MagicMock(status="new", broker_order_id="bo-1")
    tok = sign_action(accepted_sid, "unaccept", secret)
    r = client.post(f"/suggestions/{accepted_sid}/unaccept?token={tok}")
    assert r.status_code == 200
    adapter_mock.cancel_order.assert_called_once()
    # suggestion now 'cancelled'

def test_bad_token_rejected(client, accepted_sid):
    r = client.get(f"/suggestions/{accepted_sid}/unaccept?token=bad")
    assert r.status_code == 400
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_api_unaccept.py -q`
Expected: FAIL (routes 404 / not implemented).

- [ ] **Step 3: Add the templates**

`templates/unaccept_confirm.html.j2`:

```jinja
{%- import '_components.html.j2' as ui -%}
<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:Arial,Helvetica,sans-serif; color:{{ ui.BODY }}; max-width:520px; margin:0 auto; padding:24px;">
  <h2 style="color:{{ ui.INK }};">Un-accept suggestion #{{ sid }}?</h2>
  <p>{{ side | upper }} {{ "{:.0f}".format(qty) }} {{ ticker }} @ ${{ "{:,.2f}".format(limit_price) }} — current order status: <strong>{{ live_status }}</strong>.</p>
  <p style="color:{{ ui.MUTED }}; font-size:13px;">This cancels any working broker order and marks the suggestion cancelled.</p>
  <form method="post" action="/suggestions/{{ sid }}/unaccept?token={{ token }}">
    <button type="submit" style="background:{{ ui.NEG }}; color:#fff; border:0; padding:10px 18px; border-radius:6px; font-size:14px; cursor:pointer;">Confirm cancel</button>
  </form>
</body></html>
```

`templates/unaccept_result.html.j2`:

```jinja
{%- import '_components.html.j2' as ui -%}
<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif; color:{{ ui.BODY }}; max-width:520px; margin:0 auto; padding:24px;">
  <h2 style="color:{{ ui.INK }};">{{ heading }}</h2>
  <p style="color:{{ ui.MUTED }};">{{ detail }}</p>
</body></html>
```

- [ ] **Step 4: Add the routes to `main.py`**

```python
@app.get("/suggestions/{sid}/unaccept", response_class=HTMLResponse, summary="Un-accept confirm page")
def unaccept_confirm(sid: int, token: str, request: Request) -> HTMLResponse:
    from .models import OrderExecution, OrderSuggestion
    from .services.magic_link import verify_action
    settings = request.app.state.settings
    if not verify_action(sid, "unaccept", token, settings.magic_link_secret):
        raise HTTPException(status_code=400, detail="invalid or expired token")
    with session_scope() as s:
        sug = s.get(OrderSuggestion, sid)
        if sug is None:
            raise HTTPException(status_code=404, detail="suggestion not found")
        live_status = sug.status
        if sug.status == "accepted":
            exe = s.scalars(select(OrderExecution).where(
                OrderExecution.suggestion_id == sid,
                OrderExecution.dry_run.is_(False),
                OrderExecution.broker_order_id.is_not(None),
            ).order_by(OrderExecution.created_at.desc())).first()
            if exe is not None:
                adapter = request.app.state.adapters.get(sug.broker_account_id)
                if adapter is not None:
                    try:
                        live_status = adapter.get_order(exe.broker_order_id).status
                    except Exception:  # noqa: BLE001
                        live_status = "unknown (broker unreachable)"
                else:
                    live_status = "no order placed"
            else:
                live_status = "awaiting placement"
        ctx = dict(sid=sid, token=token, ticker=sug.ticker, side=sug.side,
                   qty=sug.qty, limit_price=sug.limit_price, live_status=live_status)
    return HTMLResponse(render_template("unaccept_confirm.html.j2", **ctx))


@app.post("/suggestions/{sid}/unaccept", response_class=HTMLResponse, summary="Un-accept (confirm POST)")
def unaccept_act(sid: int, token: str, request: Request) -> HTMLResponse:
    from .models import OrderSuggestion
    from .services.magic_link import verify_action
    from .services.unaccept import UnacceptResult, unaccept_suggestion
    settings = request.app.state.settings
    if not verify_action(sid, "unaccept", token, settings.magic_link_secret):
        raise HTTPException(status_code=400, detail="invalid or expired token")
    with session_scope() as s:
        sug = s.get(OrderSuggestion, sid)
        if sug is None:
            raise HTTPException(status_code=404, detail="suggestion not found")
        adapter = request.app.state.adapters.get(sug.broker_account_id)
        if adapter is None:
            raise HTTPException(status_code=404, detail="no adapter for this account")
        res = unaccept_suggestion(s, adapter, sid, broker_account_id=sug.broker_account_id)
    heading, detail = _UNACCEPT_MESSAGES[res]
    return HTMLResponse(render_template("unaccept_result.html.j2", heading=heading, detail=detail))


_UNACCEPT_MESSAGES = {
    UnacceptResult.CANCELLED: ("Un-accepted.", "Any working broker order was cancelled and the suggestion is now cancelled."),
    UnacceptResult.PARTIAL: ("Remainder cancelled.", "The unfilled remainder was cancelled; the already-filled shares stand."),
    UnacceptResult.FILLED: ("Already filled.", "The order had fully filled — nothing to un-accept. Sell manually if you want to exit."),
    UnacceptResult.NOT_ACTIONABLE: ("Not un-acceptable.", "This suggestion is not in the accepted state."),
    UnacceptResult.NOT_FOUND: ("Not found.", "No such suggestion."),
}
```

> Worker note: `UnacceptResult` is imported at module top for `_UNACCEPT_MESSAGES`; `select`,
> `HTMLResponse`, `render_template`, `session_scope`, `HTTPException`, `Request` are already
> imported in `main.py` (confirm and add any missing import).

Admin endpoint:

```python
@app.post("/admin/suggestions/{sid}/unaccept", dependencies=[Depends(admin_auth)], summary="Admin un-accept")
def admin_unaccept(sid: int, request: Request) -> dict[str, str]:
    from .models import OrderSuggestion
    from .services.unaccept import unaccept_suggestion
    with session_scope() as s:
        sug = s.get(OrderSuggestion, sid)
        if sug is None:
            raise HTTPException(status_code=404, detail="suggestion not found")
        adapter = request.app.state.adapters.get(sug.broker_account_id)
        if adapter is None:
            raise HTTPException(status_code=404, detail="no adapter for this account")
        res = unaccept_suggestion(s, adapter, sid, broker_account_id=sug.broker_account_id)
    return {"status": "ok", "id": str(sid), "result": res.value}
```

- [ ] **Step 5: Run tests + commit**

Run: `uv run pytest tests/test_api_unaccept.py -q`
Expected: PASS.

```bash
git add templates/unaccept_confirm.html.j2 templates/unaccept_result.html.j2 src/investor/main.py tests/test_api_unaccept.py
git commit -m "feat(api): un-accept confirm page + POST + admin endpoint"
```

---

## Task 7: `cancelled` status consistency + auto-trade skip guard

**Files:**
- Modify: `templates/weekly_review.html.j2` / `.txt.j2` (audit status colour for `cancelled` — it already prints `a.status`; add a colour branch)
- Test: `tests/test_auto_trade.py` (assert a `cancelled` suggestion is not re-placed)

- [ ] **Step 1: Write the failing auto-trade test**

```python
# tests/test_auto_trade.py — add
def test_cancelled_suggestion_not_replaced():
    """A suggestion marked 'cancelled' (un-accepted) is never returned for placement,
    even with a prior broker_cancelled execution in the same week."""
    # Seed: suggestion status='cancelled', week_of=current, + a dry_run=False
    # broker_cancelled execution. Assert _fetch_accepted_unexecuted(...) returns [].
    from investor.services.auto_trade import _fetch_accepted_unexecuted
    ...
```

> Worker note: `_fetch_accepted_unexecuted` already filters `status == "accepted"`, so this
> test should pass immediately — it's a **regression guard** documenting that `cancelled`
> closes the re-place footgun. If it fails, the status filter regressed.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_auto_trade.py::test_cancelled_suggestion_not_replaced -q`
Expected: PASS (guard; no code change needed for the skip).

- [ ] **Step 3: Weekly-review audit colour for `cancelled`**

In `templates/weekly_review.html.j2`, in the suggestion-audit status colour block, add a branch so `cancelled` renders in the muted/neutral colour (it currently falls through to the `else` warn colour):

```jinja
{% elif a.status == 'cancelled' %}{% set status_color = ui.MUTED %}
```
(place it alongside the existing `expired` branch).

- [ ] **Step 4: Run the email tests**

Run: `uv run pytest tests/test_email_indicators.py tests/test_email_templates.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_auto_trade.py templates/weekly_review.html.j2 templates/weekly_review.txt.j2
git commit -m "feat(unaccept): cancelled-status consistency (auto-trade guard + review audit)"
```

---

## Final verification (after all tasks)

- [ ] `uv run pytest -q` — full suite green.
- [ ] `uv run ruff check src/ tests/` — clean.
- [ ] `uv run mypy src/` — 0 errors.
- [ ] Render-preview the daily email (the `render_all.py`-style harness) with a few committed rows; eyeball the section + un-accept link, mobile width.
- [ ] Manual (dev): `POST /admin/suggestions/<sid>/unaccept` on a test accepted suggestion → returns `result`, suggestion `cancelled`, adapter `cancel_order` called; confirm-page GET makes no change.
- [ ] Update `plans/post_4_9a_changes.md` "Still open / parked" — move the un-accept item to done.

## Self-review (done while writing)

- **Spec coverage:** scope both (Task 4/5 committed rows incl. unplaced; Task 3 handles no-execution) ✓; terminal `cancelled` (Task 3) ✓; two-step confirm (Task 6 GET/POST) ✓; race refuse/partial/cancel/mark (Task 1 + 3) ✓; shared helper + expiry refactor (Task 1/2) ✓; daily section (Task 5) ✓; audit consistency + footgun (Task 7) ✓.
- **Placeholders:** test bodies referencing the in-memory DB harness are marked with worker notes pointing at the concrete sibling test to copy (`test_auto_trade.py`, existing magic-link tests) rather than inventing fixture details that vary; all production code is complete.
- **Type consistency:** `CancelOutcome`/`UnacceptResult` enum members and `CommittedOrderRow` fields are used identically across tasks; `unaccept_suggestion(session, adapter, sid, *, broker_account_id)` signature matches every call site (endpoints + admin).
