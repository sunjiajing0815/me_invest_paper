# ADR-0023 — Weekly Order Activity Metrics

**Status:** Accepted  
**Date:** 2026-05-27  
**Phase:** 4.8

## Context

Phase 4.8 adds an Order Activity section to the Friday weekly-review email. The section covers three classes of information: a suggestion funnel (counts at each lifecycle stage), dollar flow (notional routed and filled, LIVE vs. DRY_RUN), and allocation drift (per-ticker gap change Mon→Fri). Three design questions arose during implementation.

## Decisions

### 1. Allocation drift as the gap metric — not trade-attributable fill rate

**Decision:** Render `gap_pct` delta (Friday − Monday, per ticker) as the primary health metric. Do not render `$ filled ÷ $ suggested` or any trade-attributable fill-rate KPI.

**Rationale:** Trade attribution appears meaningful but breaks at the boundary of four real cases in this system:
- Manual placements: user routes in the broker UI; system has no execution row; numerator is wrong.
- Partial fills across weeks: a GTC order accepted this Monday fills next Tuesday; this week's `$ filled` = 0 despite the order being live and correct.
- Re-placement after `broker_cancelled`: versioned `client_order_id` produces a new execution row; the denominator doesn't know how to count retries.
- Organic drift: a position can move toward target purely because of market price moves, with zero execution activity.

Allocation drift measures what actually happened to the portfolio regardless of mechanism — which is what a long-term investor cares about. The seductive KPI is a fiction at this system boundary; ADR-0023 records the rejection explicitly so it is not added back as a "missing metric" in a future session.

### 2. No materialised metrics table at Phase 4.8

**Decision:** All five metric functions query live from `order_suggestion`, `order_execution`, `positions_snapshot`, and `target_allocation`. No new table; no Alembic migration.

**Rationale:** At single-user scale every metric query runs in well under 100 ms against indexed columns. A `weekly_metrics_cache` table would introduce a staleness problem (refresh logic, cache invalidation after re-runs) and a schema migration without buying any performance. The upgrade trigger is explicit: if any single metric query exceeds **500 ms** at email-send time, add `weekly_metrics_cache` populated by the same Friday job and have the email read from cache. Do not adopt the cache earlier than this threshold.

### 3. Honest accounting for the manual-placement gap

**Decision:** Surface `accepted_not_routed` as a named bucket (accepted suggestions with no `dry_run=False` execution row). Label it "presumed manual" when `auto_trade_mode == 'LIVE'`, or "auto-trade not LIVE" otherwise. Do not attempt to reconcile manual placements via `positions_snapshot` delta.

**Rationale:** The alternative — detecting manual placements by comparing `positions_snapshot` Monday vs. Friday delta against a suggestion's `qty × price` — produces false positives whenever an independent price move shifts `market_value` by the same dollar amount as the suggested quantity. At Phase 4.8, the system cannot distinguish "user bought 5 VOO manually" from "VOO gained $2.50 × existing 2 shares = same notional." The honest signal is `accepted_not_routed`; the label adapts to mode so the user is not confused during DRY_RUN soak. Phase 5+ may revisit with per-execution position deltas if that signal becomes important.
