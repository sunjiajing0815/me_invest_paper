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
