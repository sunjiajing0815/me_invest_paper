# ADR-0035 — Funds-flow detection via a cash-flow heuristic

**Status:** Accepted
**Date:** 2026-06-30 (soak-window P2.3)

## Context

The product should tell the user, by email, when money moved into or out of a broker account so
they can decide whether their targets still fit. There's no broker "transactions/transfers" feed
wired in; the available signals are the time-versioned `broker_account` state (cash/equity per
sync) and `order_execution` fills.

## Decision

**Cash-flow heuristic.** An external transfer changes cash without a matching trade, so the day's
cash change that is NOT explained by that day's fills is the external flow:

```
external_flow = (cur.cash − prev.cash) − trade_cash_flow
trade_cash_flow = Σ(sell qty×price) − Σ(buy qty×price)   over fills in (prev.last_sync, cur.last_sync]
```

- `cur` = latest `broker_account` state row; `prev` = latest row whose `last_sync` predates today
  (ET). A flow is reported only when `abs(external_flow) > FUNDS_DETECTION_THRESHOLD_USD` (default
  $500); `external_flow > 0` → deposit, `< 0` → withdrawal.
- Implemented in `services/funds.py::detect_funds_flow` (pure). The daily 18:00 ET job
  `jobs/funds_detection.py::run_funds_detection_all_brokers` persists a `funds_event` and emails a
  notice per flow. It reads the 16:15 daily-sync state (no broker call). When 2+ accounts flag a
  flow in the same run, the email carries a "consider whether these are a single transfer" note.

Rejected: **equity-attribution** (Δequity − price-driven market moves − realized PnL). More
"complete" but needs day-over-day per-position attribution and is fragile to missing snapshots; the
cash-based signal is the direct, robust indicator of external transfers.

## Consequences / known limitations

- **Dividends, interest, and fees** also change cash without a trade and will read as external flow.
  The $500 floor filters routine ones; a large dividend can still trip a (correct-but-uninteresting)
  "deposit" — the email says explicitly that a flagged flow may be a deposit, withdrawal, or
  uncategorised dividend, and asks the user to interpret. No auto-categorisation (out of scope).
- **Cross-broker transfers** surface as two events (a withdrawal at one broker, a deposit at the
  other) with the multi-account header note.
- **Multi-day gaps:** if no daily sync happened (weekend/holiday/outage), `prev` is older and the
  window spans multiple days — the flow is still correct (cumulative) but attributed to "today".
- Append-only `funds_event` (migration `c3d4e5f6a7b8`); no auto-action — surfacing only.

## References

- `services/funds.py`, `jobs/funds_detection.py`, `models.py::FundsEvent`,
  `templates/funds_event.{html,txt}.j2`. Settings: `FUNDS_DETECTION_THRESHOLD_USD` (default 500).
- Scheduled `funds_detection` Mon–Fri 18:00 ET (`scheduler.py`). Tests: `tests/test_funds_detection.py`.
