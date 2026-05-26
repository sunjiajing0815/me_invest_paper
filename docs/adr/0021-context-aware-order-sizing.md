# ADR-0021 — Context-Aware Weekly Order Sizing

**Status:** Accepted  
**Date:** 2026-05-26

## Context

Phase 4.5 added a Tavily-driven weekly market context synthesis (Fridays). ADR-0020 banned Tavily output from the suggestion engine. Phase 4.7 adds a bounded exception: the persisted Friday narrative can influence Sunday's suggestion *quantities* — within strictly bounded Python-applied limits and only via existing scored S/R level anchors.

## Decisions

### 1. Bounded context influence reverses ADR-0020's Tavily ban for quantity scaling only

ADR-0020 banned all Tavily output from the suggestion engine. That ban remains in force for:
- ticker selection (which tickers to suggest)
- price targets (what limit price to use, except via `prefer_anchor`)
- direction (buy vs. sell)
- recommendation to act

The Phase 4.7 exception is narrow: `context_adjust_node` may scale suggestion quantities by a Sonnet-derived multiplier, clamped in Python to `[context_size_min, context_size_max]` (default 0.25–1.5). The model cannot originate tickers, invent prices, or make trade recommendations. All `prefer_anchor` values must be existing method strings from `scored_levels` — validated by `_find_level()` before use.

### 2. Carved LLM output exception: bounded size multiplier is not a price target or trade recommendation

CLAUDE.md's rule "never let the LLM emit price targets, fundamental claims, or trade recommendations" applies to suggestion content. A `size_multiplier` in [0.25, 1.5] that scales shares — clamped deterministically in Python after the LLM call — is categorically different: it does not add tickers, set prices, or advise buying or selling. ADR-0013 ("LLMs judge, Python applies") is respected: the raw multiplier is never used unclamped.

### 3. Structured Finnhub earnings calendar vs. Tavily free-text `forward_events`

The earnings gate uses Finnhub's structured `earnings_calendar` endpoint, not the `forward_events` list from the Tavily synthesis. Rationale:
- Finnhub returns machine-readable `{ticker: date}` — no parsing required
- `forward_events` is free text produced by Sonnet — structurally unreliable for a deterministic gate
- The `FakeEarningsClient` makes the gate testable and fallback-safe (empty API key → no-op gate)

## Consequences

- Friday's `run_weekly_review` now persists a `WeeklyMarketContextRow` keyed to the upcoming Monday
- Sunday's `gather_context_node` loads it (within `context_max_age_days=4`) and passes it to `context_adjust_node`
- If no fresh context exists (first run, stale, or Tavily unavailable), the narrative pass is silently skipped; the earnings gate still applies if Finnhub key is set
- `context_note` on `OrderSuggestionRow` provides an audit trail: "what adjustment was made and why"
- `suggestion_critic_v2.txt` instructs the critic to respect prior defensive shrinks (rule 6)
