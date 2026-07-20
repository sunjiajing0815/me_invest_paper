# OHLCV-Aware Decision Logic — Design

**Date:** 2026-07-21 · **Status:** Approved (steps 1–2 in progress)
**Related:** `plans/post_4_9a_changes.md` (§18 on ship), ADR-0029 (split-adjusted bars),
`plans/topup_suggestions_design.md` (shared `_select_buy_anchor`)

## Problem

The bar store already holds full OHLCV (+vwap) and the S/R *computation* already uses
highs/lows (pivots, swings). But every *decision* consults only the daily **close**:
`current_price = ind.close` drives level proximity (`build_nearby_levels`), the
support/resistance side split, the 15% distance guard (`_select_buy_anchor`,
`_find_level`), sizing, and the single "Current" price tag in emails. Consequences:

- A support the day's **low pierced but the close reclaimed** (tested-and-held — bullish)
  is indistinguishable from one that was never approached.
- A support with a recent **close below it** (broken) still qualifies as a buy anchor.
- Volume — the difference between a meaningful test and noise — is never consulted.
- The email shows one number for "Current", hiding the day's range.

## Decisions (user-approved)

Keep **daily bars** (no granularity change, no Parquet/DB migration). Make the decision
layer candle-aware. All new metrics are **deterministic Python computed from bars at
runtime** — no schema migration, no new LLM output surface.

## Candle semantics matrix (the core definitions)

For a level `L` and a daily bar `(o, h, l, c, v)`:

| Event | Definition | Interpretation |
|---|---|---|
| **Touch** | `l ≤ L ≤ h` | the market actually traded at the level |
| **Tested & held** (support) | touch AND `c ≥ L` | buyers defended it — strengthens the level |
| **Broken** (support) | `c < L` | closed through — the level failed |
| **Reclaimed** | broken on an earlier bar, later bar closes back ≥ L | ambiguous; treated as *not currently broken* only if the most recent close-through is older than the lookback |
| (Resistance rows mirror: broken = `c > L`) | | |

Close remains the **market-side reference** (a support must sit at/below the close to be a
buy anchor; MA side-classification unchanged) — the candle adds *history quality*, it
doesn't move the reference point.

## New in-memory structures (no on-disk change)

```python
@dataclass(frozen=True)
class Candle:                    # services/indicators.py
    as_of: date
    open: float; high: float; low: float; close: float
    volume: float

# IndicatorRow gains: open/high/low/volume (float | None = None) — last bar's values.

@dataclass(frozen=True)
class LevelStats:                # services/levels.py
    last_touch: date | None      # most recent bar whose range included the level
    touch_count: int             # touches within LEVEL_TOUCH_WINDOW_DAYS (30)
    touched_today: bool          # last bar's range includes the level
    closed_through_recently: bool  # close beyond the level (breaking direction)
                                   # within LEVEL_BROKEN_LOOKBACK_DAYS (10)
    touch_volume_ratio: float | None  # mean volume on touch bars ÷ 20-bar mean volume

# NearbyLevels gains:
#   current: Candle | None = None                      (current_price stays — zero churn)
#   stats:   dict[tuple[str, float], LevelStats]       key = (method, round(price, 4))
#   + helper stats_for(level) -> LevelStats | None
```

Stats are computed inside `build_nearby_levels(..., bars_dir=None)` from the last ~60 bars
per ticker (DuckDB `price_bar`), **only for the nearby levels that survive filtering**
(≤6/ticker). `bars_dir=None` or any fetch failure → no stats, never a failed run.
Pure core: `compute_level_stats(bars_df, level_price, level_type, …) -> LevelStats` so the
test matrix runs on synthetic frames.

## Consumption map (later steps)

- **Step 3 (anchor guard):** `_select_buy_anchor` rejects supports with
  `closed_through_recently` (a broken level is not a pullback target); `tested_today` and
  `touch_count` flow into reason strings. Distance guard stays close-based.
- **Step 4 (LLM payloads):** `score_levels_for_ticker` and the reason node get the stats as
  structured input fields (prompt gains field descriptions only).
- **Step 5 (emails):** "Current" becomes `close (low–high)`; nearby-level cells gain
  touch markers. Weekly review's `levels_table` macro inherits.

## Config knobs (defaults; optional)

`LEVEL_TOUCH_WINDOW_DAYS=30`, `LEVEL_BROKEN_LOOKBACK_DAYS=10` (vol window fixed at 20).

## Explicitly unchanged

Bar backfill (already OHLCV; gains only a column-schema guard), indicators' close-based
math (SMA/EMA/RSI/MACD — standard), pivots/swings (already candle-based), auto-trade,
reconciliation, top-up sizing (×conf), the persisted `sr_level` schema.

## Migration

None on disk. In-memory only: new optional fields with defaults, so existing fabricators
and callers work unchanged; call sites opt in by passing `bars_dir`.
