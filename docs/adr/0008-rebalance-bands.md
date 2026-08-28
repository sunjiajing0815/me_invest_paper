# ADR-0008: Rebalance Bands are Absolute and Declared Per Ticker

**Date:** 2026-04-28 (retroactive — decision taken during Phase 0; written up 2026-08-28)
**Status:** Accepted
**Deciders:** Jane

> **Backfill note.** `plans/phase_0_guide.md` §14 asked for this decision to be written as
> ADR-0001 before Phase 1 ("absolute bands or relative bands — pick one, write three
> sentences explaining why"). It never was, and the number `0001` was subsequently used by
> every later citation to mean the broker-adapter decision instead — now
> [ADR-0001](0001-broker-adapter-abstraction.md). The band decision was nevertheless made
> and shipped; it is reconstructed here from `config/targets.yaml`, `load_targets()` in
> `src/investor/config.py`, and the band-cap logic in `services/suggest.py`. `0008` was a
> free, uncited number.

---

## Context

A target allocation of "25% VOO" is not actionable on its own. Every price move puts every
holding off its target by some amount, so the system needs a tolerance: how far may a
holding drift before it is worth suggesting an order?

Two shapes were available:

- **Relative bands** — tolerance as a fraction of the target (e.g. target ±25% of target,
  so a 25% target admits 18.75%–31.25%, while a 5% target admits 3.75%–6.25%).
- **Absolute bands** — tolerance in percentage points of portfolio, declared per ticker
  (e.g. a 25% target with a band of 21–29).

Relative bands are self-scaling: one global constant governs every position, and adding a
ticker requires no new judgement. Absolute bands are explicit: each ticker's tolerance is
visible, but each must be written down and kept consistent with its target.

## Decision

**Absolute bands, declared per ticker in `config/targets.yaml` as `band: [low, high]`,
in percentage points of total portfolio value.**

```yaml
targets:
  VOO:   { pct: 25, band: [21, 29], asset_class: index_etf }
  TQQQ:  { pct: 10, band: [7,  13], asset_class: leveraged_etf }
  MU:    { pct: 5,  band: [3,  8]  }
```

The loader enforces `band_low <= pct <= band_high` per ticker and raises on violation.
A band that does not bracket its own target is a configuration error, not a tolerated
state.

Bands are consumed in two places:

- **Reporting** — a holding outside its band reads as under/over band in the daily and
  weekly emails.
- **Sizing** — `services/suggest.py` caps every buy at the whole-share count that keeps the
  resulting holding at or below `band_high`. The band is a ceiling on order size, not only
  a trigger.

## Why absolute over relative

- **Risk is not proportional to target weight.** A leveraged ETF at 10% deserves a tighter
  tolerance than a broad index fund at 25%, because the same percentage-point drift carries
  different risk. A relative band would give TQQQ the *widest* absolute tolerance of the
  volatile holdings, which is backwards.
- **The tolerance is the interesting number.** "VOO may run 21–29" is the sentence the
  investor actually reasons about. A relative formula hides it behind arithmetic.
- **Per-ticker bands make the sizing cap legible.** Since `band_high` bounds order
  quantity, having it stated literally in the config makes it obvious why a suggestion was
  smaller than the gap.

The cost is accepted deliberately: adding a ticker means choosing two more numbers, and a
target edited without moving its band is a real failure mode.

## Consequences

**Good:**

- Per-holding risk tolerance is explicit and reviewable in one file.
- The sizing cap has an obvious, hand-set source.
- No global constant to tune, and no coupling between unrelated holdings.

**Costs:**

- Every new ticker needs a hand-chosen band, and bands drift out of agreement with targets
  if edited carelessly.
- This bit once: a target whose `pct` sat outside its own band (`QQQ pct: 25` with
  `band: [26, 34]`) loaded fine before the validator existed and made the holding read
  **perpetually under band**. That is why `load_targets()` now enforces the bracket
  invariant and raises rather than warning. See `CLAUDE.md` gotcha 29.
- Because a cash buffer is held outside the equity targets, every ticker sits marginally
  under its band-centre by construction. Documented in `CLAUDE.md` gotcha 3.

## Related

- [ADR-0007](0007-position-sizing.md) — how a gap becomes a share quantity; the band
  supplies the ceiling that sizing is capped against.
