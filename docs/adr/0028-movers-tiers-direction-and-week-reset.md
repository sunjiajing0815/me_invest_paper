# ADR-0028 — Movers Tiers are Direction-Aware and Reset per ISO Week

**Status:** Accepted
**Date:** 2026-06-09
**Commit:** `1c38fd6`

## Context

The movers job implements a tiered-threshold alert system to suppress same-direction noise
within a measurement period (a ticker that fires the 5% alert shouldn't re-alert on every
0.1% intra-day wiggle). The **MU whipsaw case** surfaced two structural bugs: MU fired a
−10.9% alert one week, bounced to +9.7% the next, and stayed silent.

- **Bug 1 — direction-blind latching.** The tier was tracked on `abs(pct)`, so a sign flip
  from −10% to +9% was treated as same-tier continuation and suppressed. The anti-spam
  machinery silenced exactly the cases — direction reversals — the user most wants to see.
- **Bug 2 — cross-week state bleed.** Tier state persisted across weeks even though the metric
  is *today vs prior-Friday close* (the baseline rolls weekly).

## Decision

Tier state now carries both:

- **Direction**, derived from the signed `last_pct_change` already stored on `mover_state`
  (no migration). A sign flip starts a fresh tier in the new direction; same-direction moves
  still escalate tier-by-tier (anti-spam preserved).
- **Measurement week**, via the ISO week (ET) of `last_triggered_at`. When the ISO
  measurement week changes, tier state resets — next week starts a fresh ladder against the
  new Friday baseline.

The logic lives in `jobs/movers.py::run_movers_email` (the tiered-threshold filter, step 3),
using the `_iso_week()` helper and the `THRESHOLD_STEP` constant. State persists on
`mover_state` (`last_triggered_threshold`, `last_pct_change`, `last_triggered_at`).

## Consequences

- Direction reversals re-alert as they should; the MU-whipsaw failure mode is closed.
- Anti-spam is preserved *within* a week.
- No schema change (direction is recovered from the existing signed `last_pct_change`).
- Expect a brief flurry of alerts in the first ISO week after deployment (transient).
- The ISO-week boundary aligns with the prior-Friday-close baseline convention; document the
  alignment so a future timezone change doesn't silently break it.
- Holiday-shortened weeks (Thanksgiving, Christmas Eve) use the same ISO boundary; verify
  behaviour the first time this hits.

## References

- `jobs/movers.py::run_movers_email` (step 3 tier filter), `_iso_week`, `THRESHOLD_STEP`.
- Same class of bug as Phase 4.8's "naive 7-day last-week math" — different module, same
  lesson: state that should reset on a calendar boundary, and signed quantities collapsed to
  magnitudes that hide direction flips.
