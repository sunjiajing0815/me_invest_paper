# ADR-0006 — Support/Resistance Methodology

**Date:** 2026-05-05  
**Status:** Accepted  
**Deciders:** Jane

---

## Context

Phase 2 adds support/resistance (S/R) levels to the daily and weekly emails. We need a reproducible, fully-deterministic methodology that can run offline, requires no manual chart-drawing, and produces levels that a long-term equity investor would recognise as meaningful.

Three classes of S/R are in common use:

| Class | Method | Subjectivity |
|---|---|---|
| Pivot points | Arithmetic formula on prior period H/L/C | None — fully deterministic |
| Moving averages | Price relative to SMA/EMA | Low — choice of period is the only parameter |
| Swing highs/lows | Fractal detection over a rolling window | Medium — lookback and confirmation bar count are tunable |

## Decision

**Implement all three classes, in priority order: pivot → MA → swing.**

Within each class:
- **Pivots:** classical floor-trader formula (P, S1, S2, R1, R2) computed on the prior trading week *and* the prior calendar month. Monthly pivots are included because many institutional desks use them.
- **Moving averages:** SMA-20, SMA-50, SMA-200, EMA-21. An MA below current price is support; above is resistance. Levels are omitted when the MA value is `None` (insufficient bars).
- **Swing levels:** fractal method with `n=5` confirmation bars. The last `n` bars of the series are always excluded — they are unconfirmed until the fractal forms.

All levels are stored per-ticker, per-method, per-date in `sr_level` with a `UniqueConstraint("ticker", "method", "as_of")`. Re-running the job on the same day is idempotent.

`build_nearby_levels` selects the 3 nearest supports (below price) and 3 nearest resistances (above price) for each ticker. This caps email length at a predictable size.

## Rationale

**Why pivots before swing?**  
Pivots are fully formulaic — identical inputs always produce identical outputs regardless of who runs the code or when. Swing detection involves a lookback window whose results shift as new bars arrive. Pivots are also universally taught and widely used as reference levels by market participants, so they are more likely to be respected.

**Why include MAs as S/R?**  
MAs are already computed for the indicators section. Re-using them as dynamic S/R is zero-cost, and the SMA-50/200 in particular are heavily cited in technical analysis as inflection zones.

**Why a 5-bar confirmation window for swings?**  
`n=5` is the most common fractal parameter in the literature. It requires two bars on each side of a candidate high/low to form before the level is locked in, reducing false positives from intraday noise on daily bars.

**Why exclude the last N bars?**  
A fractal high/low at bar `i` is only confirmed once bars `i+1` through `i+n` have all printed lower highs (or higher lows). The last N bars cannot yet be confirmed — including them would cause levels to move retroactively on each run.

## Alternatives considered

- **Fibonacci retracements** — rejected: require a subjective swing anchor selection; cannot be automated without a human identifying the "significant" swing.
- **Volume profile / VWAP bands** — deferred to Phase 4: require tick-level volume data not yet in the pipeline.
- **Kernel density estimation over historical prices** — interesting but opaque; a senior investor reviewing the email would not be able to reproduce the levels by hand.

## Consequences

- `services/levels.py` owns the full S/R pipeline.
- Adding a new level class means implementing a private `_xxx_levels()` function and wiring it into `compute_levels()` — no structural changes needed.
- Monthly pivots require at least 2 calendar months of bar history. Tickers with fewer than 2 months of data silently produce no monthly pivots (weekly pivots still fire if 2 weeks are present).
