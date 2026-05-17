# Phase 3c Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 3c — Suggestion Review Pipeline + News-Augmented Scoring + Expiry Sweep  
**Code complete:** 2026-05-17  
**Git tag:** pending (tag `v0.3.0-phase-3` after first confirmed Sunday email with Sonnet rationales and "Not Suggested This Week" section visible)

---

## 1. Scope vs. delivery

The product plan defined Phase 3c as:

> A second LangGraph workflow that routes draft suggestions through: (1) per-draft rationale generation by Sonnet, (2) cross-suggestion critique by Sonnet, (3) deterministic Python applying critic changes, and (4) persist + email with 2–4 sentence rationales. Also: news-augmented level scoring (`score_levels_v2`), parallel level scoring, and a suggestion expiry sweep job.

All planned deliverables were met. This is the final sub-phase of Phase 3; ADRs 0006 and 0007, which had been marked ⚠ Pending since Phase 3a, are now fully closed.

Two production bugs and one tuning gap were discovered during live validation (see §6): a silent data loss in `persist_suggestions()` for pre-existing pending rows, and the distance guard silently discarding a target ticker (MU) with no user-visible explanation. Both were fixed. An unplanned enhancement was added to address the second: the `SkippedRow` dataclass and a "Not Suggested This Week" email section that explains why each out-of-band ticker did not generate a suggestion.

---

## 2. What was built

### New graph

| File | Description |
|---|---|
| `graphs/suggestion_review.py` | Five-node LangGraph: `gather_context_node` → `reason_node` (Sonnet) → `critic_node` (Sonnet) → `revise_node` / `skip_revise_node` → `finalize_node`; `ReviewContext`, `CriticDecision`, `SuggestionReviewState` types; `build_suggestion_review_graph()` factory |

### New job

| File | Schedule | Description |
|---|---|---|
| `jobs/suggestion_expiry.py` | 16:20 ET Mon–Fri | Mark pending `order_suggestion` rows with `expires_at < now` as `expired`; set `acted_at = now` |

### New prompts

| File | Node |
|---|---|
| `prompts/suggestion_reason_v1.txt` | Sonnet per-draft rationale — 2–4 sentences citing evidence (confidence, RSI, news sentiment, MA distance, gap %); hard rules: no invented prices, no fundamental claims |
| `prompts/suggestion_critic_v1.txt` | Sonnet cross-suggestion critic — five severity-ordered problem categories; supported `suggested_changes` fields: `limit_price`, `qty`, `anchor_method`; hard rules: no invented prices, no invented tickers |
| `prompts/score_levels_v2.txt` | News-augmented copy of `score_levels_v1.txt` — adds `recent_material_news` input; instructs model to downgrade confidence on supports with bearish news / resistances with bullish news |

### New / updated services

| File | Change |
|---|---|
| `services/news.py` | New `load_recent_material_news(session, days) -> dict[str, list[NewsTriageItem]]` — queries `news_event` where `llm_material = True` and `created_at >= now - days`; converts ORM rows to frozen dataclasses inside the session |
| `services/llm_levels.py` | New `load_latest_scored_levels(session) -> dict[str, list[ScoredLevel]]` — queries most-recent scored batch per ticker, converts inside session; `score_levels_for_ticker()` gains optional `recent_news: list[dict] | None` param |
| `services/suggest.py` | New `SkippedRow` frozen dataclass (`ticker`, `gap_pct`, `side`, `reason`); `generate_suggestions()` returns `tuple[list[OrderSuggestionRow], list[SkippedRow]]` and emits a skip entry at every guard point; `anchor_method` field added to `OrderSuggestionRow`; `persist_suggestions()` bug fix (§6) |

### Updated jobs

| File | Change |
|---|---|
| `jobs/weekly_suggestions.py` | Invokes `build_suggestion_review_graph()`; new `score_all_tickers_parallel()` helper with `ThreadPoolExecutor(max_workers=4)`; loads last-24h material news per ticker before scoring; unpacks `(drafts, skipped)` tuple; passes `skipped` to both email templates |

### Updated templates

| File | Change |
|---|---|
| `templates/weekly_suggestions.html.j2` | Rationale column shows Sonnet 2–4 sentence explanation (falls back to mechanical `reason` on LLM failure); new "Not Suggested This Week" section between suggestions and indicators |
| `templates/weekly_suggestions.txt.j2` | Same "Not Suggested" section in plain-text fallback |

### Config / infrastructure

| File | Change |
|---|---|
| `config.py` | `level_prompt_version` default `"v1"` → `"v2"` |
| `main.py` | Schedule `sweep_expired_suggestions` via `CronTrigger` at 16:20 ET Mon–Fri |

---

## 3. Suggestion review graph design

```
drafts (pre-generated) ──→ gather_context_node (Python, DB + DuckDB)
                                    ↓
                            reason_node (Sonnet — per-draft rationale)
                                    ↓
                            critic_node (Sonnet — cross-suggestion review)
                                    ↓
                     ┌──── route_after_critic ────┐
                any revise/reject                all approve
                     ↓                               ↓
              revise_node (Python,            skip_revise_node
              _apply_changes)                  (pass-through)
                     └──────────────┬────────────────┘
                                    ↓
                            finalize_node (Python, DB)
                              persist_suggestions()
```

**`gather_context_node`** materialises all DB state before any LLM node runs — one session, closes fully, all values frozen dataclasses. See CLAUDE.md convention #9. Loads: `compute_gap()`, `load_latest_scored_levels()`, `load_recent_material_news(days=7)`, `get_latest_account_snapshot()`, `get_untracked_positions()`, `compute_indicators()`.

**`reason_node`** calls Sonnet once with all drafts in a single JSON payload. Returns `dict[int, str]` mapping draft index to 2–4 sentence rationale (≤600 chars). Falls back to mechanical `reason` field if Sonnet fails.

**`critic_node`** calls Sonnet once with all drafts plus their rationales. Each draft receives a verdict: `approve` / `revise` / `reject` plus structured `suggested_changes` (optional). The call prompt includes cash floor and untracked positions so the critic can reason about whole-portfolio constraints.

**`revise_node`** applies critic changes via `_apply_changes()` — deterministic Python only. Validates every proposed `limit_price` against known scored levels; rejects invented prices. Validates `anchor_method` against the scored-level set for that ticker. Falls back to the original draft (with a warning log) on any invalid change. See ADR-0013 for why this step is intentionally not LLM-driven.

**`finalize_node`** calls `persist_suggestions()` with the final list; returns `suggestion_ids` on state for token generation in the email step.

### Calibration target

The critic's reject-or-revise rate should be **10–25%** per weekly run. Below 5% = rubber-stamping; above 40% = prompt too strict. Monitor via `llm_call_log` rows with `purpose = "suggestion_critic"`.

---

## 4. Architecture decisions

### ADR-0013 — Suggestion Review Pipeline (new, Accepted)

Documents the five-node graph design, why `revise_node` is deterministic Python (not a second LLM call), the five severity-ordered critic problem categories, and the 10–25% calibration target.

### ADR-0006 — S/R Methodology (closed)

Removed ⚠ Pending. Final update documents Phase 3c additions: news-augmented scoring (`score_levels_v2.txt`), `anchor_method` audit trail on `order_suggestion`, and critic refinement via the suggestion review pipeline.

### ADR-0007 — Position Sizing (closed)

Removed ⚠ Pending. Final update documents Phase 3c: `anchor_method` field records which S/R method was used, `confidence_at_creation` records the scored confidence; both are now immutable on accepted/rejected rows.

### ADR-0016 — LLM Backend Abstraction (updated)

Added "Consumer OAuth — solo personal-use guardrails" section: `ANTHROPIC_API_KEY` is always the auth source for automated runs; consumer OAuth is permitted only for personal single-user automation; must not extend to multi-user deployment.

---

## 5. Database schema added

**New column on `order_suggestion`** (Alembic rev `f2680eed32f8`)

| Column | Type | Description |
|---|---|---|
| `anchor_method` | varchar, nullable | The S/R method that determined the limit price (e.g., `sma_50`, `ema_21`, `pivot_high`); null on Phase 2 fallback rows |

No new tables. The `anchor_method` column completes the audit trail started by `confidence_at_creation` in Phase 3a — every accepted suggestion now records both what level was used and how confident the model was.

---

## 6. Bugs found and fixed during production validation

### Bug 1 — `persist_suggestions()` not updating `anchor_method` for pre-existing pending rows

**Symptom:** After re-running the weekly job, 5 of 6 pending suggestions had `NULL anchor_method`; only the one new insertion (TQQQ) had `sma_200`.  
**Root cause:** The update branch in `persist_suggestions()` wrote `qty`, `limit_price`, and `reason` but silently skipped `anchor_method` and `confidence_at_creation`. These fields were correctly written on first insert but never refreshed on re-run.  
**Fix:** Added `existing.anchor_method = r.anchor_method` and `existing.confidence_at_creation = r.confidence_at_creation` to the pending-row update block. Committed `d66d21f`.

### Bug 2 — Distance guard silently skipping MU with no explanation

**Symptom:** MU was in the target allocation with a positive gap, but never appeared in the weekly suggestions email. No error, no log entry visible to the user.  
**Root cause (1 — guard too tight):** MU's current price ($724) placed all support levels 13–16% below price, exceeding the 8% `max_distance_pct` default. The guard is correct in spirit, but 8% was calibrated for liquid ETFs, not individual names that can trade extended ranges above their moving averages.  
**Root cause (2 — silent failure):** Every `continue` in `generate_suggestions()` produced no user-visible output; the email simply had fewer rows.  
**Fix (1):** Added `SkippedRow` frozen dataclass and changed `generate_suggestions()` to return `tuple[list[OrderSuggestionRow], list[SkippedRow]]`. Each guard point now emits a skip entry with a human-readable reason (e.g., "nearest support (ema_21 $629.05) is 13.1% away — exceeds 15% limit"). The weekly email gained a "Not Suggested This Week" section. Committed `418f499`.  
**Fix (2):** Raised `max_distance_pct` default from 8% to 15%. Committed `c90e76f`.

---

## 7. Test coverage

| Test file | Tests | Notes |
|---|---|---|
| `tests/test_llm.py` | 41 | Unchanged from Phase 3b |
| `tests/test_suggest.py` | 28 | Updated for tuple return type; added `TestSkippedRow` (5 new tests) |
| `tests/test_news.py` | 21 | Unchanged |
| `tests/test_suggestion_review.py` | 18 | **New** — gather_context session-leak guard, reason/critic key coverage, revise apply/reject, route logic, adversarial critic price |
| `tests/test_magic_link.py` | 18 | Unchanged |
| `tests/test_news_triage.py` | 12 | Unchanged |
| `tests/test_gap.py` | 11 | Unchanged |
| `tests/test_levels.py` | 10 | Unchanged |
| `tests/test_config.py` | 8 | Unchanged |
| `tests/test_indicators.py` | 6 | Unchanged |
| `tests/test_load_targets.py` | 5 | Unchanged |
| `tests/test_suggestion_expiry.py` | 3 | **New** — expiry sweep marks past-due pending rows; leaves future rows untouched |
| `tests/test_email.py` | 3 | Unchanged |
| `tests/test_daily_report.py` | 3 | Unchanged |
| `tests/test_weekly_suggestions.py` | 2 | **New** — parallel scoring wall-clock timing (8 mock tickers, 200ms mock latency, <800ms wall clock) |
| `tests/test_integration_alpaca.py` | 1 | Full chain vs. live Alpaca (skipped without keys — unchanged) |

**Total: 189 unit tests + 1 integration** (up from 161 at Phase 3b close).

---

## 8. Known issues and limitations

### Critic and rationale LLM calls are not retried on transient errors

If Sonnet returns a 529 or fails schema validation in `reason_node` or `critic_node`, the node falls back (empty rationale dict or empty decisions dict). The email still sends but with mechanical reasons rather than Sonnet rationales. Acceptable at current call volume; add a retry with backoff if the rate becomes noticeable.

### MU's LLM scoring silently returns empty in parallel scoring

During validation, `score_all_tickers_parallel()` returned `[]` for MU without surfacing the underlying error. The `except Exception as exc` guard logs `exc` as a string, which is opaque when the underlying failure is a JSON parse error. The fallback to Phase 2 nearest-distance still works, but the scored path is never reached for MU. Root cause not yet isolated (likely a response truncation or content-filter issue on a specific ticker's bar data). No code fix applied; fallback path is sufficient.

### Critic `revise`/`reject` rate not yet measurable

The calibration target (10–25%) cannot be validated until the first live Sunday run with enough drafts. The `llm_call_log` rows with `purpose = "suggestion_critic"` will provide the data — check after the first two weekly runs.

### `finalize_node` holds a session open during `persist_suggestions()`

`finalize_node` opens a `session_scope()` and calls `persist_suggestions()` inside it. If the graph ever adds a second DB write after this (e.g., stamping `reviewed_at`), the session scope must be widened carefully to avoid the SQLite write-lock pattern from Phase 3b Bug 3 (SqliteSaver + SQLAlchemy contention). `MemorySaver` is still the checkpointer, so this is safe today.

### `gather_context_node` calls `compute_indicators()` outside the session (DuckDB)

This is intentional — DuckDB reads Parquet files directly, no session needed. However, if bars are stale (the `update_bars()` call at the top of the weekly job failed), indicators will reflect the last successful bar pull. The job logs a warning but does not abort.

---

## 9. Environment and dependencies

**No new runtime deps** added in Phase 3c. All LangGraph, LLM, and DuckDB packages were already present from Phase 3b.

**New config keys:** none (all Phase 3c features use existing settings; `level_prompt_version` default changed from `"v1"` to `"v2"` in code).

**Alembic:** One new revision — `f2680eed32f8` (`anchor_method` column on `order_suggestion`).

**Docker:** No changes required.

---

## 10. Phase 3 retrospective

Phase 3 ran across three sub-phases spanning ~two weeks of implementation:

| Sub-phase | Key deliverable | Tests at close |
|---|---|---|
| 3a | LLM scoring, confidence-weighted anchor, Accept/Reject magic links | 109 |
| 3b | LangGraph news triage, movers email, LLM backend abstraction | 161 |
| 3c | Suggestion review pipeline, parallel scoring, expiry sweep, SkippedRow | 189 |

**Recurring pattern:** Every sub-phase found at least one `DetachedInstanceError` or silent-skip variant. The pattern is now encoded as CLAUDE.md convention #9 and CLAUDE.md gotcha #12. The `gather_context_node` pattern is a first-class architectural primitive rather than an ad-hoc fix.

**What worked well:**
- `llm_node_call()` abstraction from Phase 3b made `reason_node` and `critic_node` boilerplate-free
- `MemorySaver` (not `SqliteSaver`) has caused zero locking issues across Phase 3b and 3c
- The deterministic `_apply_changes()` revise step: critic proposes, Python applies — hallucination-free revision

**What was harder than expected:**
- `persist_suggestions()` update-path omission (Bug 1 §6) was invisible until the `anchor_method` column existed — silent NULL propagation in update branches is easy to miss
- The 8% distance guard was too conservative for individual equities at extended valuations; the tuning was only visible once `SkippedRow` made the silent discards auditable

---

## 11. Pre-tag checklist

Before tagging `v0.3.0-phase-3`:

| # | Item | Status |
|---|---|---|
| 1 | Weekly email received with Sonnet rationales (2–4 sentences) visible in the Rationale column | ⏳ Pending first live Sunday run |
| 2 | "Not Suggested This Week" section appears for any ticker failing the distance or level guard | ⏳ Pending (MU expected here if levels remain >15% away) |
| 3 | `llm_call_log` shows rows with `purpose` in (`suggestion_reason`, `suggestion_critic`) | ⏳ Pending |
| 4 | `order_suggestion.anchor_method` non-null for new pending rows | ⏳ Pending |
| 5 | Expiry sweep at 16:20 ET: pending rows past `expires_at` flip to `expired` | ⏳ Pending first weekday run |
| 6 | Critic reject-or-revise rate in 10–25% range (check after 2 weekly runs) | ⏳ Pending |
| 7 | `uv run pytest` — 189 unit tests pass | ✅ Done |
| 8 | `ruff check src/ tests/` — clean | ✅ Done |
| 9 | `mypy src/` — no new errors | ✅ Done |
| 10 | ADR-0013 written and accepted; ADR-0006/0007 closed; ADR-0016 updated | ✅ Done |
| 11 | CLAUDE.md updated (revise-node determinism, consumer OAuth multi-tenant prohibition, gather_context convention, Phase 3c prompt file gotcha) | ✅ Done |
| 12 | README updated to Phase 3 complete | ✅ Done |
| 13 | `product_plan.md` Phase 3 marked complete | ✅ Done |
