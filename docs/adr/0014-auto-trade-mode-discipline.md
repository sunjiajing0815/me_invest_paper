# ADR-0014 — Auto-Trade Mode Discipline

**Date:** 2026-05-18  
**Status:** Accepted  
**Deciders:** Jane

---

## Context

Phase 4 introduces opt-in automated order placement (`services/auto_trade.py`). The feature must
default to inert, support incremental promotion through soak stages, and protect against
misfire through hard guards and a kill switch.

## Decision

### Three-state mode

```
OFF (default) → DRY_RUN (simulate) → LIVE (real orders)
```

Mode is stored in `meta` table key `auto_trade_mode`. Default is `OFF` — the DB migration seeds
this row. Any code path that forgets to set the mode sees `OFF`, never `LIVE`.

### Default-OFF invariant

The `_get_mode()` function falls back to `"OFF"` if the `meta` row is absent or has an
unrecognised value. This makes `OFF` the fail-safe in every failure scenario.

### Single-call-site rule

`adapter.submit_order()` may only be called from `services/auto_trade.py` and `brokers/`.
No other file may call it. Enforced by a grep CI test (`tests/test_no_unauthorized_submit_order.py`).

### Promotion soak-window matrix

Promotion is gated by minimum elapsed time in the *current* mode. The clock starts when the
most recent `auto_trade_promotion_log` row with `to_mode == current_mode` was written.

| broker_scope   | to_mode  | Min days in current mode |
|----------------|----------|--------------------------|
| alpaca_paper   | DRY_RUN  | 0 (first promotion)      |
| alpaca_paper   | LIVE     | 14                       |
| alpaca_live    | LIVE     | 28                       |
| moomoo         | LIVE     | 28                       |

Demotion to `OFF` is always immediate. Promotions require the separate
`AUTO_TRADE_PROMOTION_TOKEN`; demotion and kill-switch require `ADMIN_TOKEN`.

### Idempotency via `client_order_id`

Every auto-trade order is tagged `client_order_id = f"sug-{suggestion.id}"`. The idempotency
guard in `_check_idempotency()` checks for an existing `order_execution` row with that
`client_order_id` and the same `dry_run` flag before placing a new order. Alpaca stores this
natively; Moomoo stores it in the `remark` field (see ADR-0018).

### Read-back verification (60s)

After `submit_order()` succeeds, `get_order()` is called immediately to verify the broker
echoes back the correct `client_order_id`. A mismatch triggers the kill switch
(`readback_mismatch`). This catches broker-side routing errors before they become silent data
corruption.

### Single-user-only forever

Auto-trade is designed for one portfolio owner. Phase 5 (multi-tenant) will require a
complete redesign of the mode/caps/promotion machinery. This file must not be adapted for
multi-user use without a new ADR.

## Consequences

- New env var `AUTO_TRADE_PROMOTION_TOKEN` (separate from `ADMIN_TOKEN`).
- New tables: `order_execution`, `auto_trade_promotion_log`, `kill_switch_log`, `auto_trade_caps`.
- All auto-trade rows carry `dry_run` column; all wash-sale and reconciliation queries filter
  `dry_run = false` to prevent simulated losses from affecting real trades.
- Promotion via `POST /admin/auto-trade/promote` returns 409 if soak window not met.
