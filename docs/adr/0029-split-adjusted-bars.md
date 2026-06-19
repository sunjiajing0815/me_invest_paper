# ADR-0029 — Bars Stored Split-Adjusted (`Adjustment.SPLIT`); SR-Level Re-Backfill Procedure

**Status:** Accepted — **data-semantics-changing**. Read before reading any pre-`85053ca`
bars or `sr_level` rows.
**Date:** 2026-06-08
**Commit:** `85053ca`

## Context

The weekly review for 2026-06-08 surfaced a nonsensical suggestion: ticker `BTC` showed
nearest support as `swing_low_5bar $5.96` while trading at ~$27. Three layers compounded:

1. The ticker `BTC` is the **Grayscale Bitcoin Mini Trust ETF**, not literal Bitcoin — the
   user's mental model and the engine's model had silently misaligned.
2. `BTC` did a **5:1 reverse split in November 2024**.
3. `services/bars.py::update_bars` was fetching **RAW (unadjusted)** bars (Alpaca's default),
   so pre-split 2024 history at $4–6 sat in the same Parquet file as the post-split $25–30
   current range. The fractal-low detector flagged the pre-split $5.96 as a swing low.

Verified against Alpaca: RAW $5.62 → SPLIT-adjusted $28.10 (exactly 5.0×).

## Decision

Bars are stored **split-adjusted**. `services/bars.py::update_bars` passes
`adjustment=Adjustment.SPLIT` to `StockBarsRequest`.

**Splits only, not dividends.** Splits reflect prices the market actually traded at after the
corporate action; dividend-adjusted prices represent the holder's total return, not
market-traded prices, and would distort S/R levels away from where price actually traded.

**Defence in depth.** `services/levels.py::build_nearby_levels` gains a `max_distance_pct`
parameter (default `0.50`): levels more than 50% from the current price are dropped regardless
of how they were computed, so a future regression in bar adjustment cannot resurface a
phantom-regime level.

### Re-backfill procedure (one-time, manual; executed 2026-06-08)

Stop app → back up the Parquet dir → delete `data/bars/*.parquet` → restart app →
`POST /admin/reload-targets` (full re-fetch) → `POST /admin/run-weekly-suggestions` (recompute
`sr_level`) → spot-check a historically-split ticker. Verified: BTC min low $4.4 → $22.1
(5.0×); nearest support now ~$27.80 (≈1% from current).

## Consequences

- S/R levels for historically-split tickers no longer surface phantom regimes.
- The 50%-distance filter survives any future regression in bar-adjustment handling.
- Future ticker splits are handled automatically on the next fetch.
- ⚠ **The pre/post-`85053ca` data-semantics boundary is silent.** Anyone restoring from a
  pre-06-08 Parquet snapshot must re-run the re-backfill or the inconsistency reappears.
- Dividends are deliberately not adjusted — for high-dividend ETFs (SCHD ~3.5%/yr, JEPI
  ~7%/yr) the cumulative effect over multi-year backfills is mildly stale; tracked as a
  follow-up.
- The 50%-distance filter may mask genuine signal in deep downtrends (a ticker whose only
  support is 60% below current now silently skips).

## References

- `services/bars.py::update_bars` (`adjustment=Adjustment.SPLIT`),
  `services/levels.py::build_nearby_levels` (`max_distance_pct`).
- Follow-up tracked in `plans/post_4_9a_changes.md`: ticker-name annotation in emails (surface
  "BTC = Grayscale Bitcoin Mini Trust ETF") to prevent the cognitive mismatch earlier.
