# Phase 4.8 + Post-4.8 Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 4.8 — Weekly Order Activity Summary + Post-4.8 Lifecycle Bug Fixes  
**Code complete:** 2026-05-28  
**Git tag:** pending (tag `v0.4.8.0` after 2 consecutive Friday review emails with the Order Activity section populated and every headline cross-checked against hand-written SQL)

---

## 1. Scope vs. delivery

### Phase 4.8 — Weekly Order Activity Summary

Adds a new **Order Activity** section to the Friday 17:00 ET weekly review email. Three classes of metric, all derived live from existing tables (no new schema):

1. **Suggestion funnel** — counts of suggested / accepted / routed / filled / partial / manual / rejected / expired, with DRY_RUN executions in a clearly labelled separate line.
2. **Dollar flow** — $ routed and $ filled, broken out buy vs. sell and LIVE vs. DRY_RUN.
3. **Allocation drift** — per-ticker `gap_pct` Monday vs. Friday with a "moved toward target" indicator, Monday-holiday fallback, and mid-week targets-changed footnote.

Plus a per-ticker breakdown table and a 4-week trend strip. All numbers are queried live at email-send time — no materialised cache, no new mutable state, no migration of historical data.

ADR-0023 records the three key choices: allocation drift over trade-attributable fill rate; no materialised metrics table at single-user scale; honest-accounting "not auto-routed" bucket rather than position-delta inference.

### Post-4.8 lifecycle bug fixes

Eight lifecycle correctness issues found during a comprehensive review of the suggestion/order/execution workflow. Five bugs (B1–B3, B5–B6) and three design gaps (G1–G3), in order of severity:

| # | Severity | Problem |
|---|---|---|
| B2 | High | `sug-N-rN` client_order_id raises `ValueError` in Rule 1; re-placed orders go untracked |
| B1 | Medium | Manual broker cancellation not reflected in `exec.status` (stays `accepted_for_routing`) |
| B3 | Medium | `partially_filled` activity flips suggestion to `"filled"`; GTC remainder still open |
| B5 | Medium | Mid-week target change doesn't invalidate accepted suggestions |
| B6 | Medium | Expiry sweep doesn't update `exec.status` after `cancel_order()`; stays `accepted_for_routing` |
| G1 | Low | Pending suggestions past `expires_at` show as "pending" in Friday review |
| G2 | Low | No reset path for sell suggestions; reset endpoint was buy-only |
| G3 | Low | `WeekTrendRow.filled_live` excluded partially-filled executions |

### Post-4.8 scheduler fix

The daily expiry sweep was scheduled at 16:20 ET Mon–Fri (after market close). This created a race window on Monday mornings: a stale GTC order from the prior week remained live at the broker from 09:35 ET (when auto-trade placed the new order) until 16:20 ET. Both orders could fill if the price hit the limit during that window. Fix: move the expiry sweep to **09:00 ET Mon–Fri** (pre-market), cancelling stale GTC orders before auto-trade fires at 09:35 ET.

All planned deliverables met.

---

## 2. What was built

### Phase 4.8 — New files

| File | Description |
|---|---|
| `src/investor/services/weekly_review_metrics.py` | 5 frozen dataclasses (`OrderFunnel`, `OrderFlow`, `AllocationDriftRow`, `PerTickerWeekRow`, `WeekTrendRow`) + 5 pure compute functions; no ORM rows cross session boundary |
| `src/investor/sql/funnel_counts.sql` | Suggestion funnel counts — `COUNT(DISTINCT suggestion_id)` per state to prevent GTC partial-fill double-count |
| `src/investor/sql/order_flow.sql` | Buy/sell notional routed and filled, DRY_RUN separated; `COALESCE` guards against NULL `avg_fill_price` before reconciliation runs |
| `src/investor/sql/alloc_drift.sql` | Per-ticker `current_pct_mon` and `current_pct_fri` using `MAX(snapshot_date) <= :mon/fri` fallback; `NULLIF(..., 0)` guards zero-equity edge case; `LEFT JOIN` ensures targets-without-positions show 0% |
| `src/investor/sql/per_ticker_breakdown.sql` | Per-ticker row: side suggested, qty/$ routed, qty/$ filled, drift_pp |
| `docs/adr/0023-weekly-order-activity-metrics.md` | Three decisions: allocation drift over fill-rate fiction; live queries over materialised cache; honest `accepted_not_routed` bucket |
| `tests/test_weekly_review_metrics.py` | 10 Phase 4.8 smoke tests (funnel empty/typical/DRY_RUN/manual; drift sign/over-correction/holiday/targets-changed; weekday guard; flow zeros) |

### Phase 4.8 — Updated files

| File | Change |
|---|---|
| `src/investor/jobs/weekly_review.py` | `WeeklyReview` gains 5 new fields (`order_funnel`, `order_flow`, `drift_rows`, `breakdown_rows`, `trend_rows`); `_build_review()` calls all 5 compute functions inside one read-only `session_scope`; weekday guard raises on non-Friday manual triggers |
| `src/investor/config.py` | `weekly_review_trend_weeks: int = 4`; `weekly_review_breakdown_top_n: int = 20` |
| `templates/weekly_review.html.j2` | New **Order Activity** section: funnel table, dollar-flow table, allocation-drift table (green/red drift_pp, "→ closer/farther"), per-ticker breakdown, 4-week trend strip; DRY_RUN line hidden when zero; holiday and mid-week footnotes conditional |
| `templates/weekly_review.txt.j2` | Plain-text mirror with fixed-width column alignment |
| `src/investor/queries.py` | 4 new `TextClause` constants: `funnel_counts`, `order_flow`, `alloc_drift`, `per_ticker_breakdown` — each is `text(sql_file.read_text())`, a typed handle loaded at import time. The SQL text lives exclusively in the `.sql` files; `queries.py` is the registry, not a second source of truth. |

### Post-4.8 bug fixes — Changed files

| File | Fix |
|---|---|
| `src/investor/services/reconciliation.py` | **B2**: Rule 1 ID parsing: `.split("-r")[0]` handles `sug-N-rN` retry IDs |
| `src/investor/services/reconciliation.py` | **B1**: New `_TERMINAL_STATUSES` frozenset + `sync_open_order_statuses(session, adapter)` function — polls broker for each `accepted_for_routing` execution, marks `broker_cancelled` if terminal |
| `src/investor/services/reconciliation.py` | **B3**: `persist_reconciliation()` guards suggestion flip with `r.activity.status == "filled"` — `partially_filled` no longer prematurely closes suggestion |
| `src/investor/jobs/reconciliation.py` | **B1**: Calls `sync_open_order_statuses(session, adapter)` after `persist_reconciliation()` |
| `src/investor/services/targets.py` | **B5**: After target update, expires current-week accepted suggestions for removed tickers; warns for retained tickers |
| `src/investor/jobs/suggestion_expiry.py` | **B6**: Sets `exec_row.status = "broker_cancelled"` immediately after successful `adapter.cancel_order()` |
| `src/investor/jobs/weekly_review.py` | **G1**: Before suggestion audit loop, derives `audit_status = "pending (expires Mon)"` for pending suggestions past `expires_at` |
| `src/investor/main.py` | **G2**: `reset-week-buy-suggestions` gains `side: Literal["buy", "sell", "all"] = "buy"` query param; default `"buy"` preserves backward compatibility |
| `src/investor/services/weekly_review_metrics.py` | **G3**: `WeekTrendRow.filled_live = funnel.filled_live + funnel.partial_live` |
| `src/investor/scheduler.py` | **Scheduler fix**: Expiry sweep moved from 16:20 ET to 09:00 ET Mon–Fri; `misfire_grace_time=30 min` (must complete before auto-trade at 09:35) |

### Post-4.8 bug fixes — New test files

| File | Tests |
|---|---|
| `tests/test_reset_week_suggestions.py` | **7 tests** — G2: default buy reset, sell-only reset, all-sides reset, invalid side (422), reset with no suggestions, reset with mixed sides |

---

## 3. Bug details

### B2 — Rule 1 parsing broken for retry `client_order_id`s (HIGH)

**Problem:** `int(act.client_order_id.removeprefix("sug-"))` raises `ValueError` when the ID is `"sug-5-r1"` (generated by `_next_client_order_id()` after a broker cancel). Falls to heuristic; re-placed orders went `untracked`, leaving suggestions `accepted` despite being filled.

**Fix:** `services/reconciliation.py` Rule 1 match:
```python
# Before:
sid = int(act.client_order_id.removeprefix("sug-"))
# After:
sid = int(act.client_order_id.removeprefix("sug-").split("-r")[0])
```

Handles all variants: `"sug-5"` → `5`, `"sug-5-r1"` → `5`, `"sug-123-r3"` → `123`. Truly malformed IDs still raise and fall through to heuristic matching.

---

### B1 — Manual broker cancellation not reflected in `exec.status` (MEDIUM)

**Problem:** `reconcile_activities()` skips activities with `filled_at is None`. Broker cancellations have no `filled_at`, so they were silently skipped. Execution stayed `accepted_for_routing` indefinitely.

**Fix:** New function `sync_open_order_statuses(session, adapter)` in `services/reconciliation.py`:
1. Queries all `accepted_for_routing`, `dry_run=False`, `broker_order_id IS NOT NULL` executions.
2. For each, calls `adapter.get_order(broker_order_id)`.
3. If `conf.status in _TERMINAL_STATUSES` (`"canceled"`, `"expired"`, `"rejected"`, `"done_for_day"`, `"replaced"`), sets `exe.status = "broker_cancelled"`.
4. Per-order exceptions: log warning, continue — never fails the whole run.

Called from `jobs/reconciliation.py` after `persist_reconciliation()`.

---

### B3 — Partial fill prematurely flips suggestion to `"filled"` (MEDIUM)

**Problem:** `persist_reconciliation()` flipped suggestion status for any `auto_matched` activity, including `partially_filled`. The GTC remainder was still open at the broker, but the suggestion read as done.

**Fix:** `services/reconciliation.py`:
```python
# Before:
if r.suggestion_id and r.method == "auto_matched":
# After:
if r.suggestion_id and r.method == "auto_matched" and r.activity.status == "filled":
```

---

### B5 — Mid-week target change doesn't invalidate accepted suggestions (MEDIUM)

**Problem:** `load_targets_into_db()` closed old target rows and inserted new ones but never checked `OrderSuggestion`. Auto-trade would place accepted suggestions for tickers no longer in the allocation.

**Fix:** `services/targets.py` — after inserting new target rows, finds current-week `accepted` suggestions. For each:
- Ticker **removed** from targets → `status = "expired"`, `acted_at = now` (logged WARNING)
- Ticker **retained** → logs WARNING that suggestion may be stale (limits may be stale)

---

### B6 — Expiry sweep doesn't update `exec.status` after `cancel_order()` (MEDIUM)

**Problem:** `sweep_expired_suggestions()` called `adapter.cancel_order()` but never updated `exec_row.status`. The execution permanently stayed `accepted_for_routing` even after the broker order was cancelled.

**Fix:** `jobs/suggestion_expiry.py` — one line added inside the successful cancel try-block:
```python
adapter.cancel_order(exec_row.broker_order_id)
exec_row.status = "broker_cancelled"   # ← added
cancelled += 1
```

---

### G1 — Pending suggestions past `expires_at` display as "pending" in Friday review (LOW)

**Problem:** At Friday 17:00 ET, pending suggestions have `expires_at ≈ now`. The expiry sweep fires at 09:00 ET (before this change, 16:20 ET) — either before or after the weekly review depending on timing — so they still read as "pending" in the email. They expire on Monday.

**Fix:** `jobs/weekly_review.py` — before suggestion audit loop, derives a display status:
```python
now = datetime.now(UTC)
# inside loop:
audit_status = s.status
if s.status == "pending" and s.expires_at is not None and s.expires_at <= now:
    audit_status = "pending (expires Mon)"
```

---

### G2 — No reset path for sell suggestions (LOW)

**Problem:** `POST /admin/reset-week-buy-suggestions` hard-coded `side == "buy"`. Accepted sell suggestions with live GTC orders at the broker could not be reset without manual DB intervention.

**Fix:** `main.py` — added `side: Literal["buy", "sell", "all"] = "buy"` query param. Default `"buy"` preserves backward compatibility. `"all"` resets both sides.

---

### G3 — `WeekTrendRow.filled_live` excluded partial fills (LOW)

**Problem:** `compute_4_week_trend()` set `filled_live = funnel.filled_live`, counting only fully-filled executions. Partial fills were in `funnel.partial_live` but not included in the trend.

**Fix:** `services/weekly_review_metrics.py`:
```python
# Before:
filled_live=funnel.filled_live,
# After:
filled_live=funnel.filled_live + funnel.partial_live,
```

---

### Scheduler race fix — dual GTC orders on Monday morning

**Problem:** The expiry sweep ran at 16:20 ET (post-market). Auto-trade fired at 09:35 ET. On Monday mornings, stale GTC orders from the prior week remained live at the broker from 09:35 until 16:20. Auto-trade placed a fresh GTC for the same ticker during that window. Both orders could fill if the price hit the limit.

**Fix:** `scheduler.py` — expiry sweep moved to **09:00 ET Mon–Fri**:

```
Mon-Fri timeline (old):  09:35 auto_trade → 16:20 expiry_sweep  ← race window
Mon-Fri timeline (new):  09:00 expiry_sweep → 09:35 auto_trade  ← stale orders cancelled first
```

Grace time remains 30 min (sweep must complete before 09:35 auto-trade).

---

## 4. Architecture decisions

### ADR-0023 — Weekly Order Activity Metrics (Accepted)

Three decisions:

1. **Allocation drift as the gap metric, not trade-attributable fill rate.** Fill-rate (`$ filled ÷ $ suggested`) breaks the moment a suggestion fills partially, gets re-placed after `broker_cancelled`, fills next week against a GTC order, or is placed manually. Allocation drift measures what the portfolio actually did, regardless of mechanism. The rejected alternative is recorded so it isn't added back as a "missing KPI."

2. **No materialised metrics table at Phase 4.8.** Live queries run in well under 100 ms against indexed columns at single-user scale. A cache table introduces staleness and a new schema migration. Upgrade trigger documented in the ADR: adopt if any query crosses 500 ms at email-send time.

3. **Honest accounting for the manual-placement gap.** The system surfaces `accepted_not_routed` rather than guessing. The alternative (reconciling via `positions_snapshot` delta) was rejected: false-positive matches occur when independent price moves shift `market_value` by the same dollar amount as a suggested qty. Phase 5+ may revisit.

---

## 5. Test coverage

### Phase 4.8 smoke tests (10 new)

| Test | What it verifies |
|---|---|
| `test_funnel_empty_week` | Zero-valued `OrderFunnel` — no crash, no `NULLIF` divide-by-zero |
| `test_funnel_typical_week` | Suggested/accepted/routed/filled counts match hand-written SQL |
| `test_funnel_dry_run_only` | `routed_live=0`, `dry_run_count>0`; LIVE totals are zero |
| `test_funnel_accepted_not_routed` | Accepted suggestion with no LIVE exec counts in `accepted_not_routed` |
| `test_drift_sign_under_target_moved_closer` | Under-target ticker moving up → `moved_toward_target=True` |
| `test_drift_over_correction` | Gap sign flip (over-correction) → still `moved_toward_target=True` |
| `test_drift_monday_fallback` | No Monday snapshot → uses prior day, `monday_is_fallback=True` |
| `test_drift_targets_changed_midweek` | Mid-week target insert → `targets_changed_midweek=True` |
| `test_weekday_guard_raises_on_wednesday` | Non-Friday trigger → `ValueError` raised |
| `test_order_flow_zeros_when_no_executions` | No execution rows → all flow fields zero |

### Post-4.8 bug fix tests (19 new)

| Test file | New tests | Bug |
|---|---|---|
| `tests/test_reconciliation.py` | `test_rule1_retry_order_id_sug_n_rn_matched` | B2 |
| `tests/test_reconciliation.py` | `test_partially_filled_does_not_flip_suggestion_to_filled` | B3 |
| `tests/test_reconciliation.py` | `test_filled_activity_does_flip_suggestion` | B3 |
| `tests/test_reconciliation.py` | `test_sync_open_order_statuses_marks_cancelled` | B1 |
| `tests/test_reconciliation.py` | `test_sync_open_order_statuses_ignores_live_order` | B1 |
| `tests/test_reconciliation.py` | `test_sync_open_order_statuses_tolerates_get_order_failure` | B1 |
| `tests/test_load_targets.py` | `test_target_change_expires_accepted_suggestion_for_removed_ticker` | B5 |
| `tests/test_load_targets.py` | `test_target_change_keeps_suggestion_for_retained_ticker` | B5 |
| `tests/test_suggestion_expiry.py` | `test_sweep_updates_exec_status_to_broker_cancelled_after_cancel` | B6 |
| `tests/test_suggestion_expiry.py` | `test_sweep_cancel_failure_leaves_exec_status_unchanged` | B6 |
| `tests/test_weekly_review.py` | `test_pending_past_expires_at_shows_expiry_note` | G1 |
| `tests/test_reset_week_suggestions.py` | 7 tests (new file) | G2 |
| `tests/test_weekly_review_metrics.py` | `test_trend_filled_live_includes_partial_fills` | G3 |

### Summary

| Milestone | Count |
|---|---|
| Phase 4.7 close | 298 |
| After post-4.7 updates (LLM rationale, reset endpoint, GTC hardening, reload-targets) | 305 |
| After Phase 4.8 (+10 smoke tests) | 315 |
| After B1–B6, G1–G3 bug fixes (+19 tests) | **334** |

```
uv run pytest tests/                    → 334 passed, 1 skipped
uv run ruff check src/ tests/           → clean
uv run mypy src/                        → no new errors in Phase 4.8 files
```

---

## 6. New env vars

Phase 4.8 adds two settings, both optional with defaults:

| Variable | Default | Purpose |
|---|---|---|
| `WEEKLY_REVIEW_TREND_WEEKS` | `4` | How many weeks the trend strip shows |
| `WEEKLY_REVIEW_BREAKDOWN_TOP_N` | `20` | Cap on per-ticker breakdown rows; tickers beyond top-N by `|drift_pp|` collapse to "+N more" |

No schema changes. No Alembic migrations. No Docker changes.

---

## 7. Pre-tag checklist

Before tagging `v0.4.8.0`:

| # | Item | Status |
|---|---|---|
| 1 | `uv run pytest tests/` — 334 tests pass | ✅ Done |
| 2 | `uv run ruff check src/ tests/` — clean | ✅ Done |
| 3 | ADR-0023 written and accepted | ✅ Done |
| 4 | Friday review email runs; "Order Activity" section is present in email | ⏳ Pending first live Friday run |
| 5 | Every headline in the Order Activity section cross-checked against hand-written SQL for that week | ⏳ Pending |
| 6 | Second consecutive Friday email confirms consistent behaviour | ⏳ Pending |
| 7 | `accepted_not_routed` bucket: confirm the label correctly reads "not yet routed" when auto-trade mode is DRY_RUN (not "presumed manual") | ⏳ Pending |
| 8 | Monday expiry sweep: confirm sweep fires at 09:00 ET and execution rows show `broker_cancelled` before auto-trade fires at 09:35 ET | ⏳ Pending |
| 9 | B6 in production: run `POST /admin/run-suggestion-expiry` with an expired accepted suggestion; verify `exec.status = "broker_cancelled"` in DB | ⏳ Pending |
| 10 | G2: `POST /admin/reset-week-buy-suggestions?side=sell` — confirm sell suggestion resets, buy suggestion untouched | ⏳ Pending |
