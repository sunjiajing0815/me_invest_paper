# Post-Phase 4 Update — GTC Orders & Manual Cancel

**Date:** 2026-05-22  
**Commit:** `d6085a8`  
**Scope:** Auto-trade order lifecycle improvement — no new phases, no schema changes.

---

## Problem

Phase 4 auto-trade placed limit orders with `time_in_force="day"`. DAY orders auto-cancel at 16:00 ET via the broker, so any suggestion that wasn't filled intraday was silently dropped with no record update. There was also no way to manually cancel open orders without triggering the nuclear `emergency-stop` (which also flips mode to `OFF`).

---

## What changed

### 1 — GTC limit orders (`services/auto_trade.py`)

`time_in_force` changed from `"day"` to `"gtc"`. Orders now stay open at the broker until filled or explicitly cancelled. This means:

- A buy suggestion placed Monday can still fill Tuesday–Friday if the price dips to the limit.
- No silent mid-session expiry from the broker side.

Both Alpaca (24/5) and Moomoo (extended hours 4am–8pm ET) support GTC.

### 2 — Expiry sweep cancels open GTC orders (`jobs/suggestion_expiry.py`)

The daily 16:20 ET sweep now handles `accepted` suggestions past `expires_at` as well as `pending` ones:

1. Finds `accepted` suggestions with `expires_at < now`.
2. For each, looks up the linked real `OrderExecution` row (`dry_run=False`, `broker_order_id IS NOT NULL`).
3. Calls `adapter.cancel_order(broker_order_id)` — cancel failure is logged but doesn't block expiry.
4. Marks the suggestion `expired`.

This ensures stale GTC orders don't linger at the broker past Friday 17:00 ET when new suggestions are generated Sunday.

### 3 — `POST /admin/cancel-all-orders` (`main.py`)

New lightweight admin endpoint. Cancels every `accepted_for_routing` / `dry_run=False` open order and sets `OrderExecution.status = "broker_cancelled"`. Does **not** change auto-trade mode.

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/admin/cancel-all-orders
# → {"cancelled": ["ord-abc"], "failed": [], "total_cancelled": 1, "total_failed": 0}
```

Use case: limit prices are stale mid-week (e.g., market gapped up). Cancel all → auto-trade re-places at Friday's fresh levels after run-weekly-suggestions Sunday.

### 4 — Idempotency guard updated (`services/auto_trade.py`)

Both guards (`_accepted_suggestions_not_yet_placed` and `_check_idempotency`) now exclude rows where `status = "broker_cancelled"`. This allows auto-trade to re-place a suggestion whose previous order was cancelled — whether by the expiry sweep, the new endpoint, or a manual broker cancel.

The suggestion stays `accepted` throughout; only the `OrderExecution` row status changes.

---

## Order lifecycle (updated)

```
OrderSuggestion: pending
  ↓ user accepts
OrderSuggestion: accepted

  ↓ auto-trade (09:35 ET, Mon–Fri)
OrderExecution: accepted_for_routing  (GTC order live at broker)

  ↓ filled via reconciliation
OrderExecution: filled  →  OrderSuggestion stays accepted

  ↓ OR expires_at reached (16:20 ET expiry sweep)
adapter.cancel_order()
OrderExecution: accepted_for_routing  (unchanged — broker order cancelled)
OrderSuggestion: expired

  ↓ OR admin cancel-all-orders
OrderExecution: broker_cancelled
OrderSuggestion: accepted  (re-placeable on next auto-trade run)
```

---

## Files changed

| File | Change |
|---|---|
| `src/investor/services/auto_trade.py` | `time_in_force="gtc"`; idempotency guards exclude `broker_cancelled` |
| `src/investor/jobs/suggestion_expiry.py` | Cancels GTC orders for accepted suggestions on expiry; typed `session_factory` |
| `src/investor/main.py` | New `POST /admin/cancel-all-orders`; passes `adapter` to expiry partial |
| `tests/test_suggestion_expiry.py` | 7 tests (was 3): cancel called, dry-run skipped, cancel failure tolerated, no-adapter path |
| `README.md` | New endpoint section with curl example |
| `product_plan.md` | Phase 4.6 guards updated: GTC, expiry cancellation, manual cancel |

---

## Test coverage

```
uv run pytest -m "not integration"   → 264 passed (was 260)
uv run ruff check src/ tests/        → clean
```

No schema changes. No Docker changes. No new env vars.

---

# Post-Phase 4 Update — Reload-Targets Bar Backfill + Watchlist Update

**Date:** 2026-05-27  
**Commit:** `fb21708`  
**Scope:** UX improvement to `reload-targets`; watchlist expansion; stale test fixes.

---

## Problem

After editing `targets.yaml` to add new tickers, two manual steps were required: `POST /admin/reload-targets` (to update the DB) and `uv run python scripts/backfill_bars.py` (to fetch price history). The bars step was easy to forget, which would cause the next weekly suggestions run to fail on indicator computation for the new tickers.

---

## What changed

### 1 — `reload-targets` triggers bar backfill (`main.py`)

`POST /admin/reload-targets` now calls `update_bars()` in a background thread immediately after `load_targets_into_db()` completes. New tickers get a 2-year history backfill; existing tickers get an incremental update from their last bar.

The HTTP response returns immediately with `"bars_sync": "started in background"`. Check app logs for `reload-targets: bar backfill complete` to confirm completion.

### 2 — Watchlist expanded (`config/targets.yaml`)

| Change | Detail |
|---|---|
| Removed | SCHD, AAPL |
| Added | BTC (Grayscale Bitcoin Mini Trust), ISRG (Intuitive Surgical), BRK.B (Berkshire B), GOOG |
| Adjusted | VOO and QQQ both to 25% (was 30%/25%); bands updated to `[21, 29]` |
| Fixed | `BRKB` → `BRK.B` (Alpaca canonical symbol) |

Pct sum: 95 + 5 cash = 100 ✓. All 10 tickers have `asset_class` set (index/leveraged ETFs explicit; BTC/ISRG/BRK.B/equities default to `"equity"`).

### 3 — Stale test fixes (`tests/`)

- `test_real_targets_yaml_validates`: hardcoded count `8` → `10` to match new watchlist.
- `test_wash_sale_guard_blocks_real_buy`: the loss `filled_at` was anchored to `_NOW = 2026-05-01`. As real time advanced past `_NOW + 25 days`, the 30-day wash-sale window no longer covered the loss. Changed to `datetime.now(UTC) - timedelta(days=5)` so it stays fresh indefinitely.

---

## Files changed

| File | Change |
|---|---|
| `src/investor/main.py` | `admin_reload_targets()` spawns background thread to call `update_bars()` after DB reload |
| `config/targets.yaml` | Watchlist and targets updated (10 tickers, pct rebalanced, `BRK.B` fix) |
| `tests/test_config.py` | Count assertion updated to 10 |
| `tests/test_auto_trade.py` | Wash-sale test anchored to `datetime.now(UTC)` instead of stale `_NOW` |

---

## Test coverage

```
uv run pytest tests/   → 298 passed, 1 skipped
uv run ruff check src/ tests/   → clean
```

No schema changes. No Docker changes. No new env vars.

---

# Post-Phase 4 Update — LLM Rationale Persistence + Resend Email Endpoint

**Date:** 2026-05-27
**Scope:** Persist LLM-generated rationales to DB; `resend-weekly-email` endpoint for layout testing without re-running LLM.

---

## Problem

Two related gaps:

1. LLM rationales generated by `reason_node` were ephemeral — computed each Sunday, used once for the email, then discarded. A mid-week re-run (or template fix) would re-invoke Sonnet for all tickers even if nothing had changed.
2. Testing email layout changes required either running the full Sunday pipeline (expensive, slow) or hand-crafting fixture data.

---

## What changed

### 1 — `llm_rationale` column on `order_suggestion` (migration `62b0733b198f`)

Nullable `TEXT` column. `NULL` = graph has not run for this suggestion. `NOT NULL` = rationale was written by `reason_node` and is stable for the week.

### 2 — `finalize_node` persists rationale (`graphs/suggestion_review.py`)

After `persist_suggestions()` writes/upserts rows, the node iterates `zip(finals, ids)` and sets `sug.llm_rationale` from `state["rationales"]` — same session, same transaction. Only touches `pending` rows (accepted/rejected are already protected by `persist_suggestions`).

### 3 — `reason_node` skips cached drafts (`graphs/suggestion_review.py`)

On entry, checks `state["rationales"]` for pre-seeded indices. Only sends missing drafts to Sonnet. Returns immediately if all rationales are cached (zero LLM calls). Merges cached + new before returning state.

### 4 — `weekly_suggestions.py` pre-seeds rationales

Before `graph.invoke()`, queries `llm_rationale IS NOT NULL` for the current `week_of`, builds a `{(ticker, side): rationale}` map, and pre-populates `state["rationales"]` by draft index. A mid-week re-run for a stable watchlist costs zero Sonnet calls for the reason step.

### 5 — `POST /admin/resend-weekly-email` (`main.py`)

New endpoint. Reads existing `pending`/`accepted` suggestions from DB, recomputes indicators + nearby levels (fast, no LLM), regenerates HMAC tokens, renders both HTML and text templates, and emails. Returns `404` if no suggestions exist for the current `week_of`. Subject prefixed `[Resend]` to distinguish from the real Sunday run.

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/admin/resend-weekly-email
# → {"status":"ok","week_of":"2026-05-25","suggestions_sent":4,"message":"Resent 4 suggestions for 2026-05-25"}
```

Use case: template layout change (e.g., stacked two-row rationale layout) — resend without touching LLM or regenerating suggestions.

### 6 — Stacked two-row layout in `weekly_suggestions.html.j2`

The `<colgroup>` approach for controlling rationale column width is ignored by most email clients (Gmail strips it). Replaced with a two-row layout per suggestion: row 1 = Ticker/Side/Qty/Limit/Current/~$/Actions (7 cols); row 2 = `colspan="7"` rationale cell at full container width. `<colgroup>` removed entirely.

---

## Files changed

| File | Change |
|---|---|
| `src/investor/models.py` | `llm_rationale: Mapped[str | None]` added to `OrderSuggestion` |
| `migrations/versions/62b0733b198f_*.py` | New migration — adds `llm_rationale TEXT` column |
| `src/investor/graphs/suggestion_review.py` | `reason_node`: skip cached; `finalize_node`: persist rationale; import `OrderSuggestion` |
| `src/investor/jobs/weekly_suggestions.py` | Pre-seed `state["rationales"]` from DB before graph invoke; import `OrderSuggestion`, `select` |
| `src/investor/main.py` | New `POST /admin/resend-weekly-email`; new imports for resend endpoint |
| `templates/weekly_suggestions.html.j2` | Stacked two-row layout (numbers row + full-width rationale row); `<colgroup>` removed |

---

## Test coverage

```
uv run pytest tests/   → 305 passed, 1 skipped
uv run ruff check src/ tests/   → clean
```

No Docker changes. No new env vars.

---

# Post-Phase 4 Update — Reset Buy Suggestions Endpoint

**Date:** 2026-05-27
**Scope:** Mid-week escape hatch to cancel open buy orders and return buy suggestions to pending without touching sell suggestions or triggering the kill switch.

---

## Problem

`cancel-all-orders` cancels broker orders but leaves suggestions `accepted` (by design — auto-trade re-places them). There was no way to fully walk back an accepted buy suggestion so it could be re-evaluated with fresh levels after a mid-week market move.

---

## What changed

### `POST /admin/reset-week-buy-suggestions` (`main.py`)

New admin endpoint. For every `accepted` buy suggestion in the current `week_of`:

1. Finds the linked `OrderExecution` with `status = accepted_for_routing`, `dry_run=False`, `broker_order_id IS NOT NULL`.
2. Calls `adapter.cancel_order(broker_order_id)` — cancel failure is logged but does not block the reset.
3. Sets `OrderExecution.status = "broker_cancelled"` on success.
4. Resets `OrderSuggestion.status → "pending"` and clears `acted_at`.

Sell/trim suggestions are not touched. Does **not** change auto-trade mode.

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/admin/reset-week-buy-suggestions
# → {"week_of": "2026-05-25", "suggestions_reset": [12, 13], "orders_cancelled": ["ord-abc"], "cancel_failed": []}
```

Typical flow: market gaps mid-week → `reset-week-buy-suggestions` → re-run `run-weekly-suggestions` Sunday for fresh levels → re-accept.

---

## Order lifecycle (updated)

```
  ↓ OR admin reset-week-buy-suggestions (buy side only)
OrderExecution: broker_cancelled
OrderSuggestion: pending  (re-evaluatable; appears in next resend email)
```

---

## Files changed

| File | Change |
|---|---|
| `src/investor/main.py` | New `POST /admin/reset-week-buy-suggestions`; `OrderExecution` added to top-level import |
| `README.md` | New endpoint sections for `reset-week-buy-suggestions` and `resend-weekly-email` |

---

## Test coverage

```
uv run pytest tests/   → 305 passed, 1 skipped
uv run ruff check src/ tests/   → clean
```

No schema changes. No Docker changes. No new env vars.

---

# Post-Phase 4 Update — GTC Order Lifecycle Hardening + Email DB Sync Fix

**Date:** 2026-05-27
**Scope:** Seven production bugs found after live auto-trade runs. No schema changes. No new env vars.

---

## Problems

1. **Cross-week suggestions processed**: `_fetch_accepted_unexecuted` had no `week_of` filter, so next-week suggestions (week_of=2026-06-01) were picked up during the current week's auto-trade run, hitting the per-day order count cap and generating confusing SKIPPED log lines.

2. **`cancel-all-orders` was a no-op after `reset-week-buy-suggestions`**: `cancel-all-orders` only searched for `accepted_for_routing` execution rows. After `reset-week-buy-suggestions` had already set rows to `broker_cancelled`, `cancel-all-orders` sent zero cancel requests to Alpaca — GTC orders stayed live.

3. **40010001 recovery reactivated cancelled Alpaca orders**: When Alpaca returned 40010001 (client_order_id already in use), the recovery path fetched the existing order and returned it as a valid confirmation without checking its status. Cancelled orders were re-adopted as `accepted_for_routing`.

4. **`client_order_id` permanently blocked after cancel**: Alpaca permanently blocks a `client_order_id` after any cancel. After a `reset-week-buy-suggestions`, retrying auto-trade with the same `sug-N` client_order_id always triggered 40010001, leaving the suggestion permanently un-placeable.

5. **Idempotency checks used `client_order_id` instead of `suggestion_id`**: After introducing versioned client_order_ids (sug-N-r1, sug-N-r2), both `_fetch_accepted_unexecuted` and `_check_idempotency` would miss existing execution rows keyed on the old `sug-N` id.

6. **GTC cancel-lag could place duplicate orders**: Cancelling an order sets the DB row to `broker_cancelled` before Alpaca processes the cancel. If auto-trade ran immediately after, it would place a fresh order with a new client_order_id while the old GTC order was still alive at Alpaca.

7. **Weekly suggestions email showed in-memory values that diverged from DB**: The email rendered from `state["finals"]` (in-memory, post-context_adjust). For already-`accepted` suggestions, `persist_suggestions` correctly skips the DB update — but the email still showed the freshly computed context-adjusted values. Users saw a different qty in the email than what auto-trade would place.

---

## What changed

### 1 — `week_of` filter in `_fetch_accepted_unexecuted` (`services/auto_trade.py`)

Added `as_of: date | None = None` parameter. The function now computes `current_week_monday = ref_date - timedelta(days=ref_date.weekday())` and filters `OrderSuggestion.week_of == current_week_monday`. Suggestions from prior or future weeks are never processed. `run_auto_trade_pass` accepts the same `as_of` parameter and threads it through — tests pass `as_of=_WEEK` to anchor to their fixture week.

### 2 — `cancel-all-orders` sweeps `broker_cancelled` rows too (`main.py`)

`admin_cancel_all_orders` now queries `status IN ('accepted_for_routing', 'broker_cancelled')` instead of only `accepted_for_routing`. Alpaca no-ops on already-cancelled orders; the try/except handles any errors silently. This ensures a full cancel sweep regardless of prior DB state.

### 3 — 40010001 recovery rejects non-open orders (`brokers/alpaca.py`)

After fetching the existing order on 40010001, checks `existing_status in _open_statuses` where:
```python
_open_statuses = {"new", "partially_filled", "pending_new", "accepted", "held"}
```
If the order is cancelled/filled/expired, raises `BrokerValidationError` instead of returning it as a valid confirmation. `auto_trade.py` catches `BrokerValidationError` and skips (does not kill switch).

### 4 + 5 — Versioned `client_order_id` + `suggestion_id`-based idempotency (`services/auto_trade.py`)

**`_next_client_order_id(session, sug)`**: counts prior `broker_cancelled` real execution rows for the suggestion. Returns `sug-{id}` for first attempt, `sug-{id}-r{n}` for retries. Alpaca treats each as a fresh unique order.

**`_fetch_accepted_unexecuted`**: replaced `client_order_id IN [...]` check with `suggestion_id IN [...]`. Now correctly excludes suggestions that have a non-`broker_cancelled` execution row regardless of which client_order_id variant was used.

**`_check_idempotency`**: replaced `client_order_id == f"sug-{sug.id}"` with `suggestion_id == sug.id` for the same reason.

**Post-submit path simplified**: removed the old stale-row reactivation-by-`broker_order_id` block. Always inserts a fresh execution row after a successful submit.

### 6 — GTC re-adopt pre-check before placing (`services/auto_trade.py`)

Added `_LIVE_ORDER_STATUSES` constant: `{"new", "partially_filled", "pending_new", "accepted", "held", "pending_cancel"}`.

Before building a new `OrderRequest` in LIVE mode, checks if a prior `broker_cancelled` execution row exists. If so, calls `adapter.get_order(stale.broker_order_id)`. If the order is still live, re-adopts it (`stale.status = "accepted_for_routing"`) and `continue`s — no new submission, no duplicate at broker. If the check raises or the order is closed, proceeds to place a fresh order with the versioned client_order_id.

### 7 — Weekly suggestions email reads from DB (`jobs/weekly_suggestions.py`)

After `finalize_node` returns, re-reads persisted rows from the DB using `suggestion_ids` inside a new `session_scope`. Extracts plain values (ticker, qty, base_qty, size_factor, context_note, llm_rationale, etc.) into dicts before the session closes. Email renders from these DB-sourced dicts instead of `state["finals"]`. Jinja2 dot-notation works with dicts, so no template changes needed. Guarantees the email always reflects what auto-trade will actually place.

---

## Order lifecycle (updated)

```
Retry after cancel (new):
  client_order_id = "sug-N-r{cancelled_count}"

Pre-check (new):
  stale broker_cancelled row + live order at Alpaca
    → re-adopt (no new submission)
  stale broker_cancelled row + dead/no order at Alpaca
    → place fresh order with versioned client_order_id
```

---

## Files changed

| File | Change |
|---|---|
| `src/investor/services/auto_trade.py` | `week_of` filter + `as_of` param; `suggestion_id`-based idempotency; `_next_client_order_id()`; GTC re-adopt pre-check; `_LIVE_ORDER_STATUSES`; simplified post-submit INSERT |
| `src/investor/brokers/alpaca.py` | 40010001 recovery: reject non-open orders via `_open_statuses` check |
| `src/investor/main.py` | `cancel-all-orders` sweeps `broker_cancelled` rows |
| `src/investor/jobs/weekly_suggestions.py` | Email reads back from DB after `finalize_node`; renders from DB dicts not `state["finals"]` |
| `tests/test_auto_trade.py` | `_WEEK = date(2026, 4, 27)` (proper Monday); `as_of=_WEEK` on all 20 `run_auto_trade_pass` calls |

---

## Test coverage

```
uv run pytest tests/   → 305 passed, 1 skipped
uv run ruff check src/ tests/   → clean
```

No schema changes. No Docker changes. No new env vars.
