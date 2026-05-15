# Phase 3b Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 3b — LangGraph News Triage + Movers Email + LLM Backend Abstraction  
**Code complete:** 2026-05-15  
**Git tag:** pending (tag `v0.3b.0` after first confirmed movers email with `arbitrated=true` row in `news_event`)

---

## 1. Scope vs. delivery

The product plan defined Phase 3b as:

> LangGraph-based news triage workflow for daily movers. Classify (Haiku) → critic (Haiku) → conditional arbitrate (Sonnet) pipeline. `NewsEvent` and `MoverState` ORM models. Tiered threshold movers job firing at 5%, 10%, 15%, … Email movers report with LLM-triaged headlines.

All planned deliverables were met. A v2 scope extension was added mid-phase: the `LLMClient` concrete class was pulled up to a `typing.Protocol` with two concrete implementations (`AnthropicAPIClient`, `AgentSDKClient`) and a `make_llm_client()` factory. See §4 (ADR-0016).

Four production bugs were discovered and fixed during end-to-end validation (see §6): a `DetachedInstanceError` from ORM objects escaping their session scope, a corrupted `.env` key, a `database is locked` error from shared SQLite write contention between SQLAlchemy and the LangGraph checkpointer, and an asyncio teardown error in `claude-agent-sdk` 0.1.x propagating as a `RuntimeError` that silenced news triage output.

---

## 2. What was built

### New jobs

| File | Schedule | Description |
|---|---|---|
| `jobs/movers.py` | 16:30 ET Mon–Fri | Detect ≥5% weekly movers, triage news via LangGraph, persist `NewsEvent` + `MoverState`, email report |

### New graph

| File | Description |
|---|---|
| `graphs/__init__.py` | `make_checkpointer()` — `MemorySaver` (switched from `SqliteSaver` to avoid shared SQLite write lock) |
| `graphs/_nodes.py` | `llm_node_call()` helper — single-call LLM + schema validation + `llm_call_log` persistence |
| `graphs/news_triage.py` | Three-node LangGraph: `classify_node` (Haiku batch) → `critic_node` (Haiku review) → `arbitrate_node` (Sonnet, conditional); `build_news_triage_graph()` factory |

### New services

| File | Role |
|---|---|
| `services/news.py` | `NewsRaw` dataclass + `get_news_for_movers()` — Alpaca primary, Finnhub fallback; URL normalisation via `_normalise_url()` to deduplicate cross-source Benzinga articles |

### New prompts

| File | Node |
|---|---|
| `prompts/news_classify_v1.txt` | Haiku classify — `is_material` + `sentiment` + `summary` per headline |
| `prompts/news_critic_v1.txt` | Haiku critic — flag items where Haiku may have under/over-called materiality |
| `prompts/news_arbitrate_v1.txt` | Sonnet arbitrate — re-score flagged items, final verdict |

### New templates

| File | Description |
|---|---|
| `templates/movers.html.j2` | HTML movers email — per-ticker % change, threshold hit, material news with sentiment badges |
| `templates/movers.txt.j2` | Plain-text fallback |

### LLM backend abstraction (v2 scope)

| File | Change |
|---|---|
| `services/llm.py` | `LLMClient` pulled up to `@runtime_checkable Protocol`; `AnthropicAPIClient` (renamed from `LLMClient`); new `AgentSDKClient` (async → sync bridge via `asyncio`); `make_llm_client(settings)` factory |
| `config.py` | Added `llm_backend: str`, `llm_cli_path: str` |
| `main.py` | `LLMClient(...)` → `make_llm_client(_settings)` in lifespan |
| `docs/adr/0016-llm-backend-abstraction.md` | New, Accepted |

### New admin endpoint

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/admin/run-movers` | POST | ✓ Admin | Manual trigger for the movers email job |

---

## 3. New service layer

### News triage graph design

```
raws[] → classify_node (Haiku, batch)
             ↓
         critic_node (Haiku, reviews batch)
             ↓
     ┌── flagged? ──┐
     yes            no
     ↓              ↓
 arbitrate_node   copy_to_final
 (Sonnet, per-    (pass-through)
  item re-score)
```

- `NewsTriageItem` schema: `url_hash`, `is_material`, `sentiment | None`, `summary | None`
- Critic flags items where confidence is low; Sonnet arbitrates only those items
- `arbitrated` column in `news_event` marks Sonnet-reviewed rows for audit
- All three nodes use `llm_node_call()` → `llm_call_log` row per call

### Tiered threshold logic (`movers.py`)

```
abs(pct_change) < 5%          → reset MoverState to 0.0 (recovery)
abs(pct_change) >= next_step  → process (5% on first trigger, 10% on second, …)
otherwise                     → skip (already notified at this tier)
```

`MoverState` is loaded as `dict[str, float]` (plain Python values) inside `session_scope()` before it closes — the root cause of the `DetachedInstanceError` fix in §6.

### `LLMClient` Protocol (v2)

Both backends satisfy:
- Same `LLMResponse` shape; `cost_usd` = token-level cost via `_calc_cost`
- Same daily-cost-cap semantics with midnight rollover
- Same `_strip_fences + model_validate_json` path; `(resp, None)` on parse failure
- Same `prompt_hash` = SHA-256(system+user)[:12]
- Neither backend writes `llm_call_log` rows — that's `llm_node_call()`'s responsibility

`AgentSDKClient` bridges the async `claude-agent-sdk` iterator to a sync `call()` via a manually-managed event loop (cancel pending tasks + `loop.close()`, skipping `shutdown_asyncgens()` which triggers an SDK 0.1.x bug). Safe in APScheduler threads; not safe from `async def` FastAPI routes.

---

## 4. Architecture decisions

### ADR-0011 — News Source Priority (new, Accepted)

Alpaca news is the primary source; Finnhub is the fallback when Alpaca returns no results for a ticker. URL normalisation (strip query params, lowercase host) deduplicates cross-source Benzinga articles before insertion.

### ADR-0012 — LangGraph Adoption (new, Accepted)

LangGraph chosen over a plain function pipeline for the classify→critic→arbitrate flow: the conditional edge (critic flags → Sonnet arbitrates) is cleaner as a graph than nested if-blocks, and `MemorySaver` provides within-invocation state without requiring inter-node function signatures.

### ADR-0016 — LLM Backend Abstraction (new, Accepted)

`LLMClient` pulled up to a Protocol with two implementations: `AnthropicAPIClient` (default, direct Anthropic Python SDK) and `AgentSDKClient` (wraps `claude-agent-sdk`, opt-in via `LLM_BACKEND=agent_sdk`). Factory `make_llm_client(settings)` dispatches based on `settings.llm_backend`. `LLM_CLI_PATH` allows routing through the system `claude` CLI (Pro/Max subscription session) instead of the SDK-bundled binary.

---

## 5. Database schema added

**`news_event`** — one row per news article per ticker (Alembic rev `e9abd5c02296`)

| Column | Type | Description |
|---|---|---|
| `id` | int PK | |
| `ticker` | varchar | |
| `published_at` | timestamptz | Source-reported publish time |
| `source` | varchar | `alpaca` or `finnhub` |
| `headline` | text | |
| `url` | text | |
| `url_hash` | varchar | SHA-256 of normalised URL (unique constraint) |
| `llm_material` | bool | Null if triage skipped |
| `llm_sentiment` | varchar | `bullish`/`bearish`/`neutral`/null |
| `llm_summary` | text | Null if not material |
| `llm_model` | varchar | `claude-haiku-4-5` or `claude-sonnet-4-6` |
| `llm_cost_usd` | float | Null (cost tracked via `llm_call_log`) |
| `arbitrated` | bool | True if Sonnet re-scored this item |

**`mover_state`** — one row per ticker (upserted on each movers run)

| Column | Type | Description |
|---|---|---|
| `ticker` | varchar PK | |
| `last_triggered_threshold` | float | Last % threshold that fired (5, 10, 15, …) |
| `last_triggered_at` | timestamptz | When that threshold was crossed |
| `last_pct_change` | float | Actual % change at trigger time |

---

## 6. Bugs found and fixed during production validation

### Bug 1 — `DetachedInstanceError` in `movers.py` (ORM object escaping session)

**Symptom:** `sqlalchemy.orm.exc.DetachedInstanceError: Instance <MoverState at ...> is not bound to a Session` on every movers run.  
**Root cause:** `MoverState` ORM objects were stored in `dict[str, MoverState]` inside a `session_scope()` block. The session closed at the end of the `with` block, but the dict values (live ORM proxies) were accessed on the next line outside the session.  
**Fix:** Changed dict value type to `dict[str, float]` — extracted `last_triggered_threshold` as a plain Python float while the session was still open.

### Bug 2 — Corrupted `.env` (missing newline between keys)

**Symptom:** `FinnhubAPIException(status_code: 401): Invalid API key` for every ticker.  
**Root cause:** When `LLM_BACKEND=anthropic_api` was appended to `.env`, it landed on the same line as `FINNHUB_API_KEY` without a preceding newline, making the Finnhub key value `d81a8h1r01qler4hiuag...LLM_BACKEND=anthropic_api`.  
**Fix:** Corrected `.env` to put each key on its own line.

### Bug 3 — `database is locked` (SqliteSaver vs. SQLAlchemy write contention)

**Symptom:** `WARNING:src.investor.jobs.movers:news triage graph failed for MU: database is locked` — every ticker's news triage silently returned empty results.  
**Root cause:** `SqliteSaver` (the LangGraph checkpointer) used a raw `sqlite3` connection to the same `investor.db` that SQLAlchemy's `session_scope()` held open with a write transaction (started by `session.flush()` inside `llm_node_call()`). SQLite serialises writers; the checkpointer could not acquire the write lock between nodes.  
**Fix:** Switched `make_checkpointer()` from `SqliteSaver` to `MemorySaver`. Graph checkpoint state is ephemeral (one `graph.invoke()` per ticker per run); disk persistence is not needed.

### Bug 4 — `RuntimeError: aclose(): asynchronous generator is already running` silencing LLM output

**Symptom:** `RuntimeError` propagated from `asyncio.run()` teardown into `AgentSDKClient.call()`, causing the graph node to raise, which was caught by movers.py's `except Exception` guard, leaving `final_by_ticker[ticker] = []`.  
**Root cause:** `asyncio.run()` calls `loop.shutdown_asyncgens()` during cleanup, which tries to `aclose()` the SDK's internal async generator. In `claude-agent-sdk` 0.1.x the generator is still in a semi-running state during teardown, so `aclose()` raises `RuntimeError`.  
**Fix:** Replaced `asyncio.run()` with a manual loop: `new_event_loop()` → `run_until_complete()` → cancel pending tasks → `loop.close()`. This skips `shutdown_asyncgens()` entirely.

---

## 7. Test coverage

| Test file | Tests | Coverage |
|---|---|---|
| `tests/test_config.py` | 8 | Settings + YAML loader (updated: 8 targets) |
| `tests/test_gap.py` | 11 | Gap computation (unchanged) |
| `tests/test_load_targets.py` | 5 | Hash-based target dedup (unchanged) |
| `tests/test_email.py` | 3 | FakeEmailer + SMTPEmailer (unchanged) |
| `tests/test_daily_report.py` | 3 | DailyReport (unchanged) |
| `tests/test_indicators.py` | 6 | compute_indicators() (unchanged) |
| `tests/test_levels.py` | 8 | Pivot + swing detection (unchanged) |
| `tests/test_llm.py` | 41 | `AnthropicAPIClient` (14) + `AgentSDKClient` (13) + `TestMakeLLMClient` (4) + `_strip_fences` (5) + `_calc_cost` (6) — up from 22 at Phase 3a |
| `tests/test_magic_link.py` | 12 | HMAC roundtrip + tamper/expiry variants (unchanged) |
| `tests/test_suggest.py` | 27 | Suggestion generation + `select_anchor` (unchanged) |
| `tests/test_news.py` | 27 | `get_news_for_movers()`, URL normalisation, Alpaca/Finnhub mocking, movers job orchestration |
| `tests/test_news_triage.py` | 12 | Node unit tests + graph integration (classify→critic→arbitrate, conditional routing) |
| `tests/test_integration_alpaca.py` | 1 | Full chain vs. live Alpaca (skipped without keys) |

**Total: 161 unit tests + 1 integration** (up from 109 at Phase 3a close).

---

## 8. Known issues and limitations

### `asyncio.run()` replacement is `claude-agent-sdk` 0.1.x specific

The manual event loop teardown in `AgentSDKClient.call()` works around an SDK bug. When the SDK is upgraded to 0.2.x, re-test with `asyncio.run()` first — if the `aclose()` error is gone, revert to the simpler form.

### `AgentSDKClient` not safe from async FastAPI routes

`asyncio.run()` / `new_event_loop()` raises `RuntimeError: This event loop is already running` inside `async def` routes. All current callers are APScheduler job threads (no live event loop). If a future route needs to call `llm.call()` on an `AgentSDKClient`, add an `async def call_async()` sibling method.

### No retry on transient LLM errors

Both backends return `(resp, None)` on parse failure and let the graph node fall through to the `fallback_factory`. Transient API 529s are not retried. Acceptable at current call volume.

### Movers on holidays and short weeks

The 7-day lookback window can span a holiday gap, inflating the apparent % move. Documented in CLAUDE.md gotcha #11; no code change planned.

### News deduplication is URL-hash only

Two articles from different sources with different URLs but identical content will both be inserted. Headline-level deduplication would require an LLM or embedding similarity check — out of scope for Phase 3b.

---

## 9. Environment and dependencies

**New runtime deps** (added via `uv add`):

| Package | Version | Reason |
|---|---|---|
| `langgraph` | pinned | News triage graph |
| `langchain-core` | pinned | LangGraph dependency |
| `langchain-anthropic` | pinned | Anthropic node support |
| `langgraph-checkpoint-sqlite` | pinned | Was `SqliteSaver`; now superseded by `MemorySaver` but kept for compatibility |
| `finnhub-python` | latest | News fallback |
| `claude-agent-sdk` | `>=0.1.81,<0.2` | `AgentSDKClient` backend |
| `opentelemetry-api` | `>=1.41` | Required by `claude-agent-sdk` (not declared as its dep) |

**New config keys:**

| Key | Default | Description |
|---|---|---|
| `FINNHUB_API_KEY` | — | Finnhub free tier; used as Alpaca fallback |
| `LLM_DAILY_COST_CAP_USD` | `3.0` | Updated from `1.0` in Phase 3a |
| `LLM_BACKEND` | `anthropic_api` | `agent_sdk` to route through `claude-agent-sdk` |
| `LLM_CLI_PATH` | `""` | Path to system `claude` CLI; empty = use SDK-bundled binary |

**Alembic:** One new revision — `e9abd5c02296` (`news_event` + `mover_state` tables).

**Docker:** `Dockerfile` updated to install Node.js 22 + `@anthropic-ai/claude-code` (required for `LLM_BACKEND=agent_sdk` in container). `LLM_CLI_PATH` is not set in Docker — the bundled CLI is used.

---

## 10. Recommended Phase 3c starting point

Phase 3c introduces the suggestion review pipeline and closes out ADR-0006/0007. Based on the Phase 3b foundation:

1. **News context in weekly suggestions** — `score_levels_for_ticker()` in `llm_levels.py` has a placeholder note for news headlines; Phase 3c can pass the last 24h material `news_event` rows as additional context
2. **Batched LLM calls** — the weekly job scores each ticker sequentially (~30s/ticker); Phase 3c can parallelize using `ThreadPoolExecutor` (sync `call()` is thread-safe)
3. **ADR-0006/0007 close** — document the final S/R + confidence + news-context scoring decision
4. **Suggestion expiry job** — `order_suggestion` rows with `expires_at < now()` and `status = pending` should be flipped to `expired` daily

### Files Phase 3c will primarily touch

| File | Why |
|---|---|
| `services/llm_levels.py` | Add `news_context` parameter to prompt |
| `jobs/weekly_suggestions.py` | Query recent material `news_event` rows per ticker |
| `prompts/score_levels_v1.txt` → `v2` | Include news headlines section |
| `jobs/daily_report.py` | Add suggestion expiry sweep |

---

## 11. Pre-tag checklist

Before tagging `v0.3b.0`:

| # | Item | Status |
|---|---|---|
| 1 | Movers email received with news headlines and sentiment badges | ⏳ Pending first live threshold crossing |
| 2 | `news_event` table has ≥1 row with `arbitrated = true` | ⏳ Pending Sonnet arbitration trigger |
| 3 | `mover_state` row created and threshold advances correctly on second crossing | ⏳ Pending |
| 4 | `llm_call_log` shows rows with `purpose` in (`news_classify`, `news_critic`, `news_arbitrate`) | ⏳ Pending |
| 5 | `LLM_BACKEND=agent_sdk` runs end-to-end without `RuntimeError` in Docker logs | ✅ Fixed (Bug 4) |
| 6 | `uv run pytest` — 161 unit tests pass | ✅ Done |
| 7 | `ruff check src/ tests/` — clean | ✅ Done |
| 8 | `mypy src/` — clean on changed files | ✅ Done |
| 9 | ADR-0011, ADR-0012, ADR-0016 written and accepted | ✅ Done |
| 10 | CLAUDE.md updated (Agent SDK OAuth prohibition, async bridge caveat, `LLM_BACKEND` env var, gotchas #13/#14) | ✅ Done |
