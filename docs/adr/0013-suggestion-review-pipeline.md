# ADR-0013 — Suggestion Review Pipeline

**Date:** 2026-05-17
**Status:** Accepted
**Deciders:** Jane

---

## Context

Phase 3a's suggestion engine produces mechanical buy/trim suggestions based on gap analysis and LLM-scored S/R levels. The output is accurate but explains itself in a single terse line (e.g., "underweight +4.2% — buy at sma_50 $630.27 (conf 0.78)"). There is no mechanism to catch cross-suggestion problems — two suggestions that individually look fine may together violate the cash floor, or a buy suggestion may contradict material news from the same week.

Phase 3c introduces a second LangGraph workflow that routes draft suggestions through a review pipeline before persisting.

## Decision

**Add a `graphs/suggestion_review.py` LangGraph with five nodes: gather_context → reason → critic → (revise | skip_revise) → finalize.**

### Node responsibilities

| Node | Type | Purpose |
|---|---|---|
| `gather_context` | Python, DB | Materialise gap rows, scored levels, material news, indicators, account, untracked positions into a frozen `ReviewContext` dataclass before any LLM node runs |
| `reason` | Sonnet | Write a 2–4 sentence per-draft rationale citing specific evidence (confidence, RSI, news, MA distance) |
| `critic` | Sonnet | Review all drafts as a **set**; emit approve / revise / reject with structured `suggested_changes` |
| `revise` | Python only | Apply critic's `suggested_changes` mechanically; reject changes that reference unknown levels |
| `skip_revise` | Python only | Pass-through when critic approved all drafts |
| `finalize` | Python, DB | Persist approved + revised drafts via `persist_suggestions()` |

### Why `revise` is deterministic Python and not an LLM call

The critic returns structured `suggested_changes` (e.g., `{"anchor_method": "sma_50"}`). A second LLM-driven revision step could:
1. Hallucinate additional changes the critic did not request ("while I'm at it, also change qty")
2. Introduce loop risk — the critic might then want to revise *its* revision
3. Add cost for no benefit when the change is as simple as substituting a price

Rule: **LLMs make judgments, Python applies them.** `_apply_changes()` validates every field change against known scored levels; it returns `None` (keep original) if the critic referenced an unknown method or invented price.

### Calibration target

The critic's reject-or-revise rate should fall in the range **10–25%** of drafts per weekly run.

- Below 5%: critic is rubber-stamping; the step adds two Sonnet calls per week for no value
- Above 40%: critic prompt is too strict; legitimate suggestions are being blocked

Monitor via `llm_call_log` (purpose = "suggestion_critic") and adjust the prompt rubric if rate drifts out of range for more than two weeks.

### `gather_context_node` and the DetachedInstanceError pattern

The `gather_context_node` opens ONE session, calls all DB loaders inside it, closes the session, then computes indicators (DuckDB, no session needed) outside the closed block. Every loader must return frozen dataclasses or plain Python — no ORM objects leave the `with session_scope():` block.

This is the third occurrence of the DetachedInstanceError pattern in this codebase:
1. Phase 1 Bug 2 — `BrokerAccount` → `AccountSnapshot`
2. Phase 3b Bug 1 — `MoverState` → `dict[str, float]`
3. Phase 3c — `gather_context_node` (this ADR's subject)

The fix is always the same: project to a frozen dataclass at the session boundary.

### `finalize_node` and persist_suggestions

`finalize_node` calls `persist_suggestions(s, finals, targets_id, week_of)` and stores the returned IDs in `state["suggestion_ids"]`. The weekly job reads `result["suggestion_ids"]` to generate HMAC magic-link tokens for the email. This avoids a second DB query after graph invocation.

### Rationale in the weekly email

The email template uses `item.rationale` (Sonnet-written) as the visible reason column, falling back to `suggestion.reason` (mechanical) when the LLM reason node fails. The mechanical reason remains in `order_suggestion.reason` in the DB as the immutable audit trail.

## Consequences

- Two Sonnet calls (reason + critic) are added per weekly run: ~$0.04 at current volumes. Well within the `llm_daily_cost_cap_usd` budget.
- The critic may reject legitimate suggestions; the calibration target (10–25%) guides prompt tuning.
- `MemorySaver` is the checkpointer — reasoning traces are ephemeral. Audit trail lives in `llm_call_log` (purpose, cost, status) and `order_suggestion` (final rationale, status lifecycle). This is the same trade-off as Phase 3b's news triage graph.
- `anchor_method` is now a first-class field on `OrderSuggestionRow` and `order_suggestion` (Alembic migration `f2680eed32f8`). Older rows have `anchor_method = NULL`.
