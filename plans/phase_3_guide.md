# Phase 3 — Overview & Sub-Phase Index

Phase 3 is the largest single phase of the project (LLM scoring, accept/reject workflow, news triage, suggestion review). To keep each unit shippable and tag-worthy, it's broken into three sub-phases that follow the dependency graph below. Each sub-phase has its own step-by-step guide.

```
Phase 3a — Foundational LLM + Scored Levels + Accept/Reject  (~2 weeks)
   │
   ├──▶ Phase 3b — News Triage via LangGraph                  (~1 week)
   │
   └──▶ Phase 3c — Suggestion Review via LangGraph            (~1.5 weeks)
                                                                 ▲
                                                                 │
                                                              depends on 3b
                                                              (reads news_event)
```

| Sub-phase | Scope | Tag on completion | Detailed guide |
|---|---|---|---|
| **3a** | `services/llm.py` cost-guarded LLM wrapper + `llm_call_log` table. Sonnet 4.6 single-call level scoring → confidence-weighted anchor selection. `PATCH /suggestions/{id}` + HMAC-signed magic-link Accept/Reject buttons in the weekly email. | `v0.3a.0` | [`phase_3a_guide.md`](phase_3a_guide.md) |
| **3b** | LangGraph introduced. News triage graph: classify (Haiku) → critic (Haiku) → conditional arbitrate (Sonnet). New `news_event` table. Daily 16:30 ET movers email when something moved ≥ 5 %. ADR-0012 anchors the LangGraph-or-not decision rule. | `v0.3b.0` | [`phase_3b_guide.md`](phase_3b_guide.md) |
| **3c** | Second LangGraph workflow: suggestion review. Sonnet writes per-draft rationale → Sonnet critic reviews drafts as a set → deterministic Python `revise` applies critic's structured changes → finalize + persist. Email rationales upgrade from mechanical single lines to 2–4 sentence explanations. Final close-out of ADRs 0006 + 0007. Tag `v0.3.0-phase-3` closes all of Phase 3. | `v0.3.0-phase-3` | [`phase_3c_guide.md`](phase_3c_guide.md) |

## Dependency rationale

**3a is the foundation.** Everything in 3b and 3c depends on the LLM client wrapper, cost guard, JSON-schema validation pattern, and prompt versioning convention introduced in 3a. The level scoring (3a) is also a direct input to 3c's suggestion-review graph. Accept/reject (3a) is parallel to the LLM work but ships in the same sub-phase because both are small enough that one tag makes sense.

**3b is the LangGraph foothold.** It's the right place to introduce LangGraph because news triage has a clear critic-step benefit (catches over-classified analyst noise, sentiment/summary mismatches) and the workflow is simple — three nodes plus a conditional edge. The dev experience of running, inspecting checkpoints, debugging prompt issues is established here before the more complex 3c graph.

**3c is the synthesis.** It uses scored levels from 3a, news context from 3b, the existing gap engine, and indicators — and routes them through a reason → critic → revise pipeline before any suggestion reaches the user. This is the sub-phase where rationales in the weekly email shift from mechanical single lines to thoughtful 2–4 sentence explanations.

## What ships at each tag

| Tag | User-visible behaviour |
|---|---|
| `v0.3a.0` | Weekly suggestions email has limit prices chosen by LLM confidence rather than nearest distance. Accept/Reject buttons in the email work. |
| `v0.3b.0` | New daily movers email arrives on days a watchlist ticker moved ≥ 5 % vs. last week's close, with material headlines summarised by Claude. |
| `v0.3.0-phase-3` | Weekly suggestions email shows 2–4 sentence rationales reflecting full context (gap, levels, news, indicators). Critic visibly rejects or revises low-quality drafts before they reach the user. |

## Total time budget

~3–4 weeks (14–18 evenings). Most of the work is in 3a (~2 weeks) and 3c (~1.5 weeks). 3b is the lightest at ~1 week.

## ADRs across the three sub-phases

| ADR | Status after sub-phase | Sub-phase where written |
|---|---|---|
| `0006-sr-methodology` | Phase 2: ⚠ Pending → 3a: partial update → 3c: final close | 3c |
| `0007-position-sizing` | Phase 2: ⚠ Pending → 3a: partial update → 3c: final close | 3c |
| `0009-llm-guardrails` | new | 3a |
| `0010-magic-link-auth` | new | 3a |
| `0011-news-source-priority` | new | 3b |
| `0012-langgraph-adoption` | new (decision rule for when to graph vs. single call) | 3b |
| `0013-suggestion-review-pipeline` | new | 3c |

---

*This file is an index. Open the sub-phase guide for actual implementation steps.*
