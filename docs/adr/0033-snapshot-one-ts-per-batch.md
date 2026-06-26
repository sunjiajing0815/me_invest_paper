# ADR-0033 — Snapshot one-`ts`-per-batch contract

**Status:** Accepted (documents an invariant enforced since `fffa6dc`, 2026-06-13)
**Date:** 2026-06-23
**Commit:** `fffa6dc` (enforcement); this ADR codifies the contract

## Context

A position sync writes one `positions_snapshot` row per held ticker. Several batch-aggregation
queries select "the latest sync" by its timestamp. The canonical one is
`src/investor/sql/alloc_drift.sql`, which picks the most recent batch with
`… AND ts = (SELECT MAX(ts) FROM positions_snapshot WHERE …)`.

This assumes **every row written by one sync shares a single `ts`**. The Moomoo weekly-review
drift table surfaced what happens when that assumption breaks (post-4.9a §9): the Moomoo adapter
called `datetime.now()` **per position**, so a single 15-row sync produced 15 distinct
microsecond timestamps. `ts = MAX(ts)` then matched exactly **one** row (a non-target ticker),
so every target ticker LEFT-JOINed to NULL and the entire drift table read `0.0`. Alpaca was
unaffected only because its adapter happened to capture `now()` once for the batch.

## Decision

**Every row in one sync batch must share one `ts`.** The service layer enforces it: 
`services/snapshot.py::take_snapshot` tags **every** `PositionsSnapshot` row with
`account.as_of` (the single account-level timestamp), never the per-position `p.as_of`.

This is a **broker-adapter contract**: an adapter's `get_positions()` must stamp all positions
in a call with one timestamp — capture `now()` **once** before the loop, not per position. The
service-layer `account.as_of` tagging is the backstop that makes the invariant hold regardless,
but adapters should still behave correctly (e.g. the Alpaca and Moomoo adapters capture `now()`
once; see `brokers/moomoo.py::get_positions`).

## Consequences

- `alloc_drift.sql`'s `ts = MAX(ts)` batch selection is correct: one `ts` ⇒ one full batch.
- The per-ticker "latest" queries (`positions_latest.sql`, `gap_allocation.sql`,
  `untracked_positions.sql`) use `ROW_NUMBER() … PARTITION BY ticker ORDER BY ts DESC`, which is
  robust to multiple timestamps — but they still rely on the invariant to mean "one coherent
  portfolio snapshot" rather than a mix of rows from different syncs.
- **New broker adapters in Phase 4.9c (IBKR, Tiger) must honor this.** Per-row `as_of` would
  silently reintroduce the §9 all-zero drift bug. This is the first thing to check when wiring a
  new adapter's `get_positions()`.
- A one-time historical backfill (in `fffa6dc`) collapsed each past account-62 sync — each a
  clean single-second batch — to one `ts` so prior weeks re-render correctly.

## References

- `services/snapshot.py::take_snapshot` (tags all rows with `account.as_of`).
- `brokers/moomoo.py::get_positions` (captures `now()` once before the loop).
- `src/investor/sql/alloc_drift.sql` (`ts = MAX(ts)` — the query that depends on this).
- post-4.9a changelog §9 (`plans/post_4_9a_changes.md`); test
  `tests/test_snapshot.py` (distinct per-row `as_of` → all written rows share one `ts`).
