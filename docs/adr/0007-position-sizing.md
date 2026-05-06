# ADR-0007 — Position Sizing for Weekly Order Suggestions

**Date:** 2026-05-05  
**Status:** Accepted  
**Deciders:** Jane

---

## Context

The order-suggestion engine (`services/suggest.py`) needs a rule to translate "this ticker is under-target by X dollars" into a concrete share quantity and limit price. The rule must:

1. Reduce drift without committing all available capital at once.
2. Produce whole or fractional share quantities that Alpaca can accept.
3. Be configurable without code changes.
4. Match the risk appetite of a long-term buy-and-hold investor — conservative, not aggressive.

## Decision

**Default to `HALF_THE_GAP`: buy/sell half the dollar gap in a single order.**

```
gap_usd = abs(target_allocation_usd − current_allocation_usd)
order_usd = gap_usd / 2
qty = floor(order_usd / limit_price)  # rounded down to avoid over-allocation
limit_price = nearest_S/R_level       # below price for buys; above for sells
```

The rule is encapsulated in a `SizingRule` dataclass and a `HALF_THE_GAP` constant so callers can override it:

```python
@dataclass
class SizingRule:
    fraction: float = 0.5  # fraction of gap_usd to deploy

HALF_THE_GAP = SizingRule(fraction=0.5)
```

Two safety guards prevent the suggestion from being generated at all:
- **Cash floor:** after the buy, remaining cash must be ≥ `cash_floor` (default $100).
- **Distance guard:** the S/R level must be within `max_distance_pct` (default 8%) of the current price.

`persist_suggestions` enforces an additional invariant: it never overwrites a suggestion whose `status` is `accepted` or `rejected`. Re-running the weekly job is idempotent for acted-upon rows.

## Rationale

**Why half-the-gap and not full-gap?**  
A full-gap order deploys the entire shortfall in one trade. For long-term holdings this is often fine, but it leaves no room to buy more if the price drops further after the order fills. Half-the-gap is a modest averaging-in strategy — it closes meaningful drift while preserving optionality.

**Why not a fixed dollar amount?**  
Fixed-dollar sizing ignores position weight entirely. A $500 buy in a $200k portfolio has a very different effect than a $500 buy in a $10k portfolio. `HALF_THE_GAP` is portfolio-aware because `gap_usd` is computed from actual target weights.

**Why a configurable `SizingRule` abstraction?**  
Providing a named dataclass instead of hardcoding `0.5` makes it easy to experiment (e.g., `SizingRule(fraction=1.0)` for a full-gap aggressive mode) without forking the engine logic.

**Why round quantity down (floor)?**  
Fractional share support varies by broker. Rounding down guarantees we never exceed the intended dollar amount. The residual (< 1 share worth) is acceptable slippage for a suggest-only system.

**Why an S/R level as the limit price?**  
Using a known support (for buys) or resistance (for sells) as the limit price serves two purposes: it anchors the order near a technically meaningful level and it naturally skips the suggestion when no level is in range (the distance guard fires).

## Options evaluated

| Rule | Verdict | Reason |
|---|---|---|
| Full gap (`fraction=1.0`) | Rejected | Too aggressive; all-in on one signal |
| Fixed dollar (`$500/order`) | Rejected | Portfolio-size-blind |
| Half gap (chosen) | Accepted | Closes drift without full commitment |
| Kelly criterion | Deferred | Requires win-rate estimates; not available in Phase 2 |
| Volatility-scaled | Deferred | Requires ATR or similar; adds complexity for marginal gain |

## Consequences

- Tickers with no nearby S/R level within `max_distance_pct` produce no suggestion. This is intentional — it is preferable to skip a week than to suggest a trade anchored at an arbitrary price.
- The `SizingRule` abstraction means a Phase 3 agent could replace `HALF_THE_GAP` with a volatility-scaled rule without touching the engine's control flow.
- The cash floor guard means the system will not suggest orders that would leave the account below a minimum operating balance.
