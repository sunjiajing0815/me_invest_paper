# ADR-0012 — LangGraph Adoption

**Date:** 2026-05-12
**Status:** Accepted
**Deciders:** Jane

---

## Context

Phase 3b introduces a news triage workflow that classifies each article as material or not, then applies a critic step that can flag borderline items for re-evaluation. A single LLM call can classify, but has no self-correction mechanism — if the initial classification is low-confidence there is no way to route it differently without a second conditional call.

LangGraph provides graph-based orchestration with named nodes, typed edges, and built-in `SqliteSaver` checkpointing. The alternative — hand-rolling a `while` loop with explicit state dicts — produces equivalent behaviour but lacks checkpointing and becomes harder to extend when more steps are added.

The key architectural question is when to use a graph versus a single LLM call.

## Decision

**Use LangGraph for the news triage workflow. Use single LLM calls (no graph) for structured input → structured output with no judgment between steps.**

The news triage graph has three nodes:

1. **classify** — assigns `material: bool` and `confidence: float` to an article.
2. **critic** — reviews classify output; emits `flag: bool` when confidence is below threshold or the classification looks inconsistent.
3. **arbitrate** — conditional node reached only when `flag=true`; produces final `material` verdict.

The edge from critic to arbitrate is conditional. This is precisely the case described by the decision rule below.

### Decision rule for future steps

> **Use a graph when you would write a Python `if` to decide whether to run another LLM call — that is a graph edge.**

`score_levels_for_ticker()` (Phase 3a) takes structured input and returns structured output with no branching between LLM calls. It stays as a plain function and is not migrated to a graph.

### Conventions

| Convention | Detail |
|---|---|
| Checkpointing backend | `MemorySaver()` — in-memory, per-`graph.invoke()`. See **Phase 3b postscript** below for why `SqliteSaver` was abandoned. |
| `thread_id` format | `f"news-{ticker}-{date}"` (e.g., `news-AAPL-2026-05-12`) |
| Commits inside nodes | Never commit inside a graph node; commit after `graph.invoke()` returns. With `MemorySaver` the write-lock contention is gone, but the rule still holds for clarity. |
| Context into graph | Use a dedicated `gather_context_node` that runs first, reads all ORM data into a frozen dataclass, closes the session, and puts the dataclass on state. Downstream LLM nodes never open a session. See CLAUDE.md convention #9. |
| State mutation | Never mutate LangGraph node state in place; always return `{**state, "new_key": value}` — in-place mutation works in unit tests but breaks checkpointing |
| Dependency pinning | Version-pin `langgraph`, `langchain-core`, `langchain-anthropic`, and `langgraph-checkpoint-sqlite` to specific minor versions; upgrade deliberately. The ecosystem has historically been version-churn-prone. |

### Calibration target

The critic-flagging rate should fall in the range **10–30%** of classified articles.

- Below 5%: the critic is rubber-stamping every classify result; the step adds latency without value.
- Above 50%: the critic threshold is too strict; arbitration cost will dominate.

Monitor the rate via the `critic_flagged` column on `news_article` and adjust the critic prompt if the rate drifts out of range for more than one week.

## Consequences

- The graph adds one or two extra LLM calls for flagged articles. At the expected flagging rate (10–30%) and current article volumes, the additional cost is within the `llm_daily_cost_cap_usd` budget.
- With `MemorySaver`, a partially-completed triage run cannot resume after a crash — articles already classified are re-classified on retry. At current volumes (≤20 articles per ticker per run) this is acceptable; the alternative (`SqliteSaver`) caused production failures (see postscript).
- Adopting LangGraph couples the news pipeline to a rapidly-evolving dependency. Version-pinning (see conventions above) is the mitigation; plan a deliberate upgrade review each quarter.
- Plain single-call functions such as `score_levels_for_ticker()` are not affected by this ADR. The "use a graph when you'd write a Python `if`" rule keeps scope contained.

---

## Phase 3b postscript — why `SqliteSaver` was abandoned

The original design used `SqliteSaver(sqlite3.connect(db_path, check_same_thread=False))` for checkpointing. This was replaced with `MemorySaver()` in Phase 3b after a production `database is locked` failure with the following root cause:

1. A graph node calls `llm_node_call()`, which calls `session.flush()` to persist an `llm_call_log` row.
2. `session.flush()` starts a write transaction on SQLAlchemy's connection to `investor.db`.
3. LangGraph attempts to checkpoint node output via `SqliteSaver`, using a *separate* `sqlite3` connection to the same `investor.db`.
4. SQLite serialises writers — the second connection cannot acquire the write lock while the first holds it.
5. Result: `OperationalError: database is locked`; the graph node raises; news triage silently returns empty results.

The fix is architectural: `MemorySaver` has no SQLite connection and no write lock. Checkpoint state is ephemeral and held in Python memory for the duration of one `graph.invoke()` call, which is all this codebase requires.

**Do not revert to `SqliteSaver` in Phase 3c or later.** If crash-recovery for long-running graphs becomes necessary, the correct path is a dedicated checkpoint database (separate file from `investor.db`), not sharing the OLTP file.
