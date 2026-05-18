# ADR-0017 — Reconciliation Matching

**Date:** 2026-05-18  
**Status:** Accepted  
**Deciders:** Jane

---

## Context

The reconciliation service (`services/reconciliation.py`) must match broker fill activities
to existing `order_suggestion` rows. Fills may originate from the auto-trade engine (where
the match is exact) or from manual trades placed in the broker UI (where we must heuristically
infer a match or record the fill as untracked).

## Decision

### Four matching rules (priority order)

| Priority | Rule | Match method | Confidence |
|----------|------|--------------|------------|
| 1 | `client_order_id.startswith("sug-")` — auto-trade placed | `auto_matched` | 1.0 |
| 2 | Single accepted suggestion with matching ticker + side within 48h of suggestion `created_at`, price within ±0.5% | `auto_matched` | 0.9 |
| 3 | Multiple candidates satisfying Rule 2 criteria | `manual_review` | 0.5 |
| 4 | No matching candidate | `untracked` | 0.0 |

Rules 2 and 3 use a directional window: `0 ≤ (fill_filled_at − suggestion_created_at).seconds < 48*3600`. This prevents fills before a suggestion was created from being matched.

### `sug-N` namespace reservation

`client_order_id` values starting with `"sug-"` are reserved for the auto-trade engine.
Manual orders in the broker UI must never use this prefix (there is no enforcement beyond
convention, but violation would cause a false Rule 1 match).

### 1-hour overlap window

The reconciliation job pulls activities with `since = last_successful_run_at - 1h`. The
broker adapter internally applies the same overlap window (Alpaca activities can have
timestamp rounding; Moomoo uses UTC conversion). This means fills near the last-run
boundary are fetched twice but the `(broker_order_id, broker)` unique constraint on
`order_execution` prevents duplicate rows.

### FIFO cost basis

`compute_realized_pnl()` calculates realised PnL for sell fills using FIFO:

1. Select all prior buy executions for the same ticker where `filled_at ≤ sell.filled_at`,
   `dry_run = False`, and `filled_qty > 0`, ordered by `filled_at ASC`.
2. Apply each buy lot against the sell quantity until exhausted.
3. `pnl = proceeds − total_cost` (may be negative).
4. If no buy history is found, returns `None` with a WARNING log (no crash).
5. If sell qty exceeds total buy history, logs a WARNING and computes pnl against matched
   portion only.

The `dry_run = False` filter is critical — simulated losses from DRY_RUN passes must never
affect the FIFO cost basis for real trades.

### Persist semantics

`persist_reconciliation()` uses an upsert-like pattern:

- If an existing `order_execution` row with the same `(broker_order_id, broker)` and
  `dry_run = False` is found → UPDATE fill fields only (preserve existing `match_method`
  unless it was the first write).
- Otherwise → INSERT a new row with `dry_run = False`.

`dry_run = True` rows are **never** matched by this lookup. They are invisible to
reconciliation, preventing DRY_RUN records from absorbing real fills.

### Partial-fill handling

For partially filled orders, the `Activity.status` field carries `"partially_filled"`.
`persist_reconciliation()` stores this status verbatim. A subsequent reconciliation run
for the final fill will UPDATE the row's `filled_qty` and flip status to `"filled"`.

## Consequences

- `order_execution.match_method` carries the reconciliation quality signal for audit.
- `manual_review` rows surface in the Friday weekly review email for user inspection.
- `untracked` rows capture manual trades that the engine did not generate — useful for
  honest PnL accounting but not linked to any suggestion.
- `POST /admin/reconcile/{execution_id}` allows the user to manually promote a
  `manual_review` row to a specific suggestion_id with `match_method='manual_matched'`.
