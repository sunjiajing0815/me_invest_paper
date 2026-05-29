# Phase 4.8 Post-Review Fixes — Completion

## Summary

A code reviewer identified seven gaps in the Phase 4.8 lifecycle bug fixes. All seven were remediated in a single follow-up commit. Three fixes closed critical correctness/safety holes, two improved architectural robustness, and two were cosmetic cleanups. Tests grew from 334 → 342 (+8 new tests).

---

## What was fixed

### Fix 1 — B5: Cancel live GTC orders on target change (CRITICAL)

**Problem:** `load_targets_into_db()` expired accepted suggestions when a ticker was removed from targets but never cancelled the linked broker GTC orders. A live order could fill between the targets edit and the 09:00 ET expiry sweep.

**Changes:**
- `services/targets.py`: added `adapter: BrokerAdapter | None = None` keyword param; after expiring a suggestion, queries `OrderExecution` for `accepted_for_routing` / `dry_run=False` rows and calls `adapter.cancel_order()` on each; cancel failures are logged as warnings (expiry sweep retries at 09:00 ET); fixed stale-ticker warning text from "may be stale — targets changed mid-week" → "sizing may be stale — qty was computed against the old target pct"
- `main.py`: `admin_reload_targets()` now passes `adapter=request.app.state.adapter`

**Tests added:** `test_target_change_cancels_live_order_for_removed_ticker`, `test_target_change_no_adapter_does_not_crash`

---

### Fix 2 — B6: Verify cancel confirmation before setting broker_cancelled (CRITICAL)

**Problem:** After `cancel_order()` returned, `exec_row.status = "broker_cancelled"` was set unconditionally. An order filled between the cancel request and Alpaca processing it would be marked `broker_cancelled` despite having filled.

**Changes:**
- `jobs/suggestion_expiry.py`: after `cancel_order()` succeeds, calls `adapter.get_order()` to verify; if `conf.status == "filled"` logs a warning and leaves exec status untouched (reconciliation handles it); if `get_order()` raises, conservatively assumes cancel succeeded (logged as warning); `cancelled` counter only increments in the non-filled path

**Tests added/updated:** `test_sweep_does_not_set_broker_cancelled_when_order_already_filled` (new); updated `test_sweep_updates_exec_status_to_broker_cancelled_after_cancel` to mock `get_order()` returning `status="canceled"`

---

### Fix 3 — Structural: Stale live-order guard in auto_trade (HIGH)

**Problem:** Moving the expiry sweep to 09:00 ET reduces the race window but doesn't eliminate it (missed sweeps, container restarts, NTP skew). A structural guard in auto_trade ensures the safety property is enforced at placement time regardless of sweep timing.

**Changes:**
- `services/auto_trade.py`: added `_check_stale_live_order(session, sug)` guard — raises `_GuardFailure` if any `accepted_for_routing` / `dry_run=False` execution exists for the same ticker from a DIFFERENT suggestion; called immediately after `_check_idempotency()` in the per-suggestion loop

**Tests added:** `test_stale_live_order_blocks_placement`

---

### Fix 4 — B2: Shared `_parse_suggestion_id` helper (MEDIUM)

**Problem:** `int(cid.removeprefix("sug-").split("-r")[0])` in Rule 1 of reconciliation was brittle inline parsing duplicated across callsites. Malformed IDs like `"sug-foo-r1"` were silently swallowed by a bare `except ValueError`.

**Changes:**
- `services/reconciliation.py`: added `import re`; added `_SUG_ID_RE = re.compile(r"^sug-(?P<id>\d+)(?:-r\d+)?$")` and `_parse_suggestion_id(client_order_id: str) -> int | None` helper; replaced the try/except block in Rule 1 with `sid = _parse_suggestion_id(cid); if sid is not None: ... # malformed → fall through`

**Tests added:** `test_rule1_malformed_sug_id_falls_through_to_heuristic`

---

### Fix 5 — B1: Batch `list_orders` replaces N-round-trip polling (MEDIUM)

**Problem:** `sync_open_order_statuses()` called `get_order()` once per execution row — O(N) broker round trips. The "replaced" terminal status also silently lost the new order's ID.

**Changes:**
- `brokers/base.py`: added `list_orders(self, status: str) -> list[OrderConfirmation]` to `BrokerAdapter` Protocol
- `brokers/alpaca.py`: implemented `list_orders()` using `GetOrdersRequest(status=QueryOrderStatus(status), limit=500)` with `cast(AlpacaOrder, _o)` for mypy correctness
- `brokers/moomoo.py`: implemented `list_orders()` mapping `"open"` → `[WAITING_SUBMIT, SUBMITTING, SUBMITTED]` and `"closed"` → `[FILLED_ALL, CANCELLED_ALL, FAILED, EXPIRED]` via `order_list_query(status_filter_list=...)`
- `services/reconciliation.py`: rewrote `sync_open_order_statuses()` to call `adapter.list_orders(status="closed")` once, build a `{broker_order_id: conf}` lookup dict, then iterate open executions O(1) per row; added docstring noting "replaced" treatment and fill-miss sequencing invariant

**Tests added/updated:** `test_sync_uses_batch_list_orders` (new — verifies `get_order` never called, `list_orders` called once); updated 3 existing sync tests to mock `list_orders` instead of `get_order`

---

### Fix 6 — G2: Canonical `/admin/reset-week-suggestions` route (LOW)

**Problem:** The endpoint URL `reset-week-buy-suggestions` was misleading since it now handles `side=sell` and `side=all` too.

**Changes:**
- `main.py`: stacked a second `@app.post("/admin/reset-week-suggestions", ...)` decorator on the same handler function; old path retained as backward-compat alias

**Tests added:** 3 new tests in `test_reset_week_suggestions.py` covering canonical URL with `side=buy`, `side=sell`, `side=all`

---

### Fix 7 — G3: Rename "Filled" → "Filled+∂" in trend table (LOW)

**Problem:** The 4-week trend table "Filled" column now includes partial fills (`filled_live + partial_live`), but the header still said "Filled", confusing readers comparing it to the current-week funnel.

**Changes:**
- `templates/weekly_review.html.j2`: trend table header `"Filled"` → `"Filled+&#x2202;"` (line 332)
- `templates/weekly_review.txt.j2`: trend table header `"Filled"` → `"Filled+∂"` (line 117)

---

## Test summary

| Milestone | Tests |
|---|---|
| Phase 4.8 (bug fixes + metrics) | 334 |
| Post-review fixes (+8) | **342** |

1 pre-existing failure in `test_weekday_guard_raises_on_wednesday` (module-reload + datetime patch interaction, unrelated to this work).

---

## Files changed

| File | Fix |
|---|---|
| `src/investor/services/targets.py` | B5 |
| `src/investor/main.py` | B5, G2 |
| `src/investor/jobs/suggestion_expiry.py` | B6 |
| `src/investor/services/auto_trade.py` | Structural |
| `src/investor/services/reconciliation.py` | B2, B1 |
| `src/investor/brokers/base.py` | B1 |
| `src/investor/brokers/alpaca.py` | B1 |
| `src/investor/brokers/moomoo.py` | B1 |
| `templates/weekly_review.html.j2` | G3 |
| `templates/weekly_review.txt.j2` | G3 |
| `tests/test_load_targets.py` | B5 |
| `tests/test_suggestion_expiry.py` | B6 |
| `tests/test_auto_trade.py` | Structural |
| `tests/test_reconciliation.py` | B2, B1 |
| `tests/test_reset_week_suggestions.py` | G2 |

---

## Pre-tag punch list (unchanged from phase_4_8_completion.md)

- [ ] First live Friday review email received and inspected
- [ ] Trend table shows 4 weeks of data (requires 4 Fridays in LIVE mode)
- [ ] `sync_open_order_statuses()` + `reconcile_activities()` both run cleanly Monday
- [ ] `sweep_expired_suggestions` runs cleanly at 09:00 ET Monday
- [ ] Auto-trade LIVE mode places ≥1 real order and reconciliation matches it
- [ ] Moomoo parallel-run soak: ≥4 weeks of clean `moomoo_parallel` logs
