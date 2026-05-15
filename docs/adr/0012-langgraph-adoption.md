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
| Checkpointing backend | `SqliteSaver(sqlite3.connect(db_path, check_same_thread=False))` — use direct constructor, not `SqliteSaver.from_conn_string` which is a `@contextmanager` and cannot be used at module level |
| `thread_id` format | `f"news-{ticker}-{date}"` (e.g., `news-AAPL-2026-05-12`) |
| Commits inside nodes | Never commit inside a graph node — `SqliteSaver` and the OLTP engine share the SQLite write lock; commit after `graph.invoke()` returns |
| State mutation | Never mutate LangGraph node state in place; always return `{**state, "new_key": value}` — in-place mutation works in unit tests but breaks checkpointing |
| Dependency pinning | Version-pin `langgraph`, `langchain-core`, `langchain-anthropic`, and `langgraph-checkpoint-sqlite` to specific minor versions; upgrade deliberately. The ecosystem has historically been version-churn-prone. |

### Calibration target

The critic-flagging rate should fall in the range **10–30%** of classified articles.

- Below 5%: the critic is rubber-stamping every classify result; the step adds latency without value.
- Above 50%: the critic threshold is too strict; arbitration cost will dominate.

Monitor the rate via the `critic_flagged` column on `news_article` and adjust the critic prompt if the rate drifts out of range for more than one week.

## Consequences

- The graph adds one or two extra LLM calls for flagged articles. At the expected flagging rate (10–30%) and current article volumes, the additional cost is within the `llm_daily_cost_cap_usd` budget.
- `SqliteSaver` checkpointing means a partially-completed triage run can resume after a crash without re-classifying already-processed articles. This is the primary reason to accept the LangGraph dependency.
- The `SqliteSaver` tables (`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`) are written into `investor.db` alongside OLTP tables. They are not managed by Alembic; LangGraph creates them on first use. Do not reference them in hand-written queries.
- The SQLite single-writer constraint (ADR-0003) applies within graph nodes: no node may call `session.commit()` while `SqliteSaver` may be writing. Commit only after `graph.invoke()` returns and the saver context is closed.
- Adopting LangGraph couples the news pipeline to a rapidly-evolving dependency. Version-pinning (see conventions above) is the mitigation; plan a deliberate upgrade review each quarter.
- Plain single-call functions such as `score_levels_for_ticker()` are not affected by this ADR. The "use a graph when you'd write a Python `if`" rule keeps scope contained.
