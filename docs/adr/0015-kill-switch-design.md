# ADR-0015 — Kill-Switch Design

**Date:** 2026-05-18  
**Status:** Accepted  
**Deciders:** Jane

---

## Context

The auto-trade engine must self-protect against scenarios where automated order placement
becomes unsafe or inconsistent with intent. A kill switch must halt all automated activity
instantly, cancel recent open orders, and require manual re-authorisation to resume.

## Decision

### Four triggers

| Trigger             | Cause                                                                           |
|---------------------|---------------------------------------------------------------------------------|
| `broker_error`      | `adapter.submit_order()` raised an exception                                   |
| `readback_mismatch` | `adapter.get_order()` returned a different `client_order_id` than was submitted |
| `readback_failed`   | `adapter.get_order()` raised an exception after a successful `submit_order()`  |
| `manual`            | `POST /admin/auto-trade/emergency-stop` called by the user                     |

Per-suggestion guard rejections (`_GuardFailure` — wash-sale, caps, idempotency, cash
insufficiency) do **not** trigger the kill switch. They skip the affected suggestion and
record a `rejected_reason` in the outcome, but leave mode intact. Only the four triggers
above are severe enough to halt the entire engine.

### Kill-switch actions (executed atomically within one `session_scope()`)

1. Flip `meta.auto_trade_mode = 'OFF'`.
2. Query `order_execution` for rows where `match_method='auto_trade_placed'`,
   `dry_run=False`, `status='accepted_for_routing'`, `created_at >= now - 24h`.
3. Call `adapter.cancel_order()` for each such row. Log errors but continue.
4. Write a `KillSwitchLog` row with trigger, detail, and cancelled order IDs (JSON).
5. Send an alert email to `settings.email_to`.

### Recovery is always manual

There is no automatic recovery or retry after a kill switch fires. The operator must:
1. Investigate the cause from `kill_switch_log` and application logs.
2. Resolve the underlying issue.
3. Re-promote via `POST /admin/auto-trade/promote` (soak clock restarts from 0 days for the
   current mode on the re-promotion).

### Audit permanence

`kill_switch_log` rows are never deleted. They are a permanent audit record of every kill
switch event and must be preserved for post-mortem analysis.

## Consequences

- `_trigger_kill_switch()` in `services/auto_trade.py` implements all five actions.
- `POST /admin/auto-trade/emergency-stop` requires only `ADMIN_TOKEN` (not the promotion
  token) to allow rapid manual intervention.
- Any code path that handles a `broker_error`, `readback_mismatch`, or `readback_failed`
  must call `_trigger_kill_switch()` and then `break` out of the suggestion loop —
  do not continue processing further suggestions after a kill switch.
