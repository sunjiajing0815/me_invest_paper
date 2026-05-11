# ADR-0009 — LLM Guardrails for Level Scoring

**Date:** 2026-05-11
**Status:** Accepted
**Deciders:** Jane

---

## Context

Phase 3a introduces Claude Sonnet 4.6 to assign confidence scores [0.0, 1.0] to computed S/R levels. Without explicit guardrails the model could invent price levels not present in `computed_levels`, issue buy/sell trade recommendations, or make fundamental claims (earnings, revenue, guidance) that fall outside the system's suggest-only product boundary.

Additionally, unbounded API usage against a production Anthropic key would allow a bug in the scoring loop to exhaust the daily budget silently.

## Decision

**Four guardrails apply to every call through `score_levels_for_ticker()`.**

### 1. Input-bounded scoring only

The system prompt instructs the model to score only the levels supplied in the request payload. The prompt explicitly forbids the model from suggesting additional price levels. If the model's response references a price not present in the input, the Pydantic validator rejects the entire response and `score_levels_for_ticker()` returns `[]`.

### 2. No trade recommendations

The system prompt forbids any buy, sell, or hold language. The allowed output is a JSON array of `{level_id, confidence}` objects — nothing else. Any prose or recommendation language in the response causes the Pydantic schema validation to fail.

### 3. No fundamental claims

The prompt explicitly states that the model must not reference earnings, revenue, guidance, analyst targets, or any fundamental data. The model receives only ticker symbol, current price, and OHLCV bars — it has no fundamental context to draw on in any case.

### 4. JSON-schema validation as the safety boundary

`score_levels_for_ticker()` passes the raw API response through a Pydantic model before the caller ever sees it. A schema breach is logged at `WARNING` level and the function returns `[]`, triggering the nearest-distance fallback in `generate_suggestions()`. The weekly job is never interrupted by a malformed LLM response.

### Cost cap

`Settings.llm_daily_cost_cap_usd` (default `5.0`) is checked before each scoring batch. If the running total for the UTC day already exceeds the cap, `score_levels_for_ticker()` returns `[]` immediately without making an API call. This prevents a runaway loop from exhausting the budget overnight.

### Model pinning

The Anthropic client is constructed with an explicit model ID (`claude-sonnet-4-6`). The string `"latest"` or any non-versioned alias is never used. An unrecognised model string raises `KeyError` at startup, not at runtime, so misconfiguration is caught immediately.

## Consequences

- LLM failures are always safe: `score_levels_for_ticker()` either returns a valid scored list or `[]`; callers must treat `[]` as "no confidence data available" and fall back to nearest-distance selection.
- Rotating the Anthropic key or bumping the model version requires a deliberate change to `.env` and `config.py` — accidental drift to a new model is not possible.
- The `$5/day` default cap is intentionally conservative for a single-user deployment. It can be raised in `.env` without a code change.
- Schema breaches are logged but do not raise in the weekly job. Monitor `WARNING` lines containing `score_levels_for_ticker` to detect persistent prompt-injection attempts or upstream model behaviour changes.
