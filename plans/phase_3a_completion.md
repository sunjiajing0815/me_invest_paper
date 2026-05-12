# Phase 3a Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 3a — Foundational LLM + Scored Levels + Accept/Reject  
**Code complete:** 2026-05-12  
**Git tag:** pending (tag `v0.3a.0` after first confirmed Sunday email with LLM confidence scores)

---

## 1. Scope vs. delivery

The product plan defined Phase 3a as:

> Shared LLM client wrapper (`services/llm.py`) with daily cost cap, JSON-schema validation, and call-log persistence. Claude Sonnet 4.6 single-call level scoring → confidence-weighted anchor selection in the weekly suggestions engine. `PATCH /suggestions/{id}` + HMAC-signed magic-link Accept/Reject buttons in the weekly email.

All planned deliverables were met. Three production bugs were discovered and fixed during validation (see §6): output truncation from an undersized `max_tokens`, Claude wrapping JSON output in markdown fences despite the "no preamble" instruction, and the error capture in `llm_call_log.error` logging the raw (fenced) content rather than the actual parse failure.

One unplanned enhancement: the suggestion `reason` string was extended to include the LLM confidence score and rationale inline, making the scoring visible in the weekly email without requiring a separate column.

---

## 2. What was built

### New endpoints

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/suggestions/{id}` | PATCH | ✓ Admin | Accept or reject a suggestion programmatically; returns 409 if already acted |
| `/suggestions/{sid}/{action}` | GET | HMAC token | Magic-link endpoint for Accept/Reject buttons in the weekly email; returns HTML confirmation |

### New services

| File | Role |
|---|---|
| `services/llm.py` | `LLMClient` (cost guard, day-rollover, schema validation) + `LLMResponse` + `persist_llm_call_log()` + `_strip_fences()` |
| `services/llm_levels.py` | `ScoredLevel` frozen dataclass + `score_levels_for_ticker()` — calls Sonnet, validates schema, drops invented methods, clamps confidence, truncates rationale |
| `services/magic_link.py` | `sign_action()` / `verify_action()` — HMAC-SHA256 over `{sid}:{action}:{expires}`, constant-time compare |
| `prompts/score_levels_v1.txt` | Sonnet scoring prompt with three hard rules, ≤20-word rationale constraint, future-news placeholder note |

### Updated services

| File | Change |
|---|---|
| `services/suggest.py` | Added `select_anchor()`, extended `OrderSuggestionRow` with `confidence_at_creation`, extended `generate_suggestions()` with optional `scored_levels` param, updated `persist_suggestions()` to return `list[int]` and write `confidence_at_creation`; reason string now includes `(conf X.XX)` and LLM rationale when scored path succeeds |
| `jobs/weekly_suggestions.py` | Added `llm: LLMClient` parameter, per-ticker scoring loop, token generation for email buttons |
| `main.py` | `app.state.llm` created in lifespan, `weekly_fn` partial updated, two new endpoints added |
| `templates/weekly_suggestions.html.j2` | Accept (green) and Reject (grey) inline buttons per suggestion row |

### Database schema added

**`llm_call_log`** — one row per LLM API call

| Column | Type | Description |
|---|---|---|
| `ts` | timestamptz | Call timestamp (UTC) |
| `purpose` | varchar | e.g. `score_levels` |
| `model` | varchar | e.g. `claude-sonnet-4-6` |
| `prompt_hash` | varchar | First 12 hex chars of SHA-256(system+user) |
| `input_tokens` | int | Prompt tokens consumed |
| `output_tokens` | int | Completion tokens produced |
| `cost_usd` | float | Estimated USD cost |
| `latency_ms` | int | Wall-clock latency |
| `status` | varchar | `ok` / `schema_error` / `api_error` |
| `error` | text | Actual parse/API error message (populated on failure) |

**New nullable columns on `sr_level`:** `confidence`, `llm_rationale`, `scored_at`, `scored_by_model`, `prompt_version`

**New nullable columns on `order_suggestion`:** `confidence_at_creation`, `acted_at`, `note`

### Weekly suggestions email changes

The suggestions table gained two columns:

- **Actions** — green Accept and grey Reject buttons, each linking to `GET /suggestions/{sid}/{action}?token=...`
- **Reason** — now includes `(conf X.XX)` and the LLM rationale when scoring succeeded, e.g.:

  > `underweight +40.0% — buy at sma_50 $630.27 (conf 0.78), Tested twice as support in 30 days. closes ~50% of gap`

  Falls back to the Phase 2 format (no confidence, no rationale) when LLM scoring fails.

---

## 3. New service layer

### `LLMClient` design

- Single instance per app lifetime, stored on `app.state.llm`
- `_spent_today` resets on calendar day boundary (checked before every call)
- Default daily cap: **$1.00 USD** (overridable via `LLM_DAILY_COST_CAP_USD`)
- `call()` returns `tuple[LLMResponse, T | None]` — caller always checks second element for `None` before use
- Schema validation strips markdown fences (`_strip_fences()`) and extracts the outermost `{...}` before passing to Pydantic — guards against Sonnet wrapping output in ` ```json ``` ` regardless of instructions
- Model constants pinned: `HAIKU = "claude-haiku-4-5"`, `SONNET = "claude-sonnet-4-6"` — never use `"latest"` aliases

### `score_levels_for_ticker()` defences

1. Pre-filters `computed_levels` to those within **30%** of current price before sending to LLM (levels further than 30% are never selected by `select_anchor()`'s 8% band and waste output tokens)
2. Unknown `method` strings in LLM output silently dropped (LLM cannot invent levels)
3. `confidence` clamped to `[0.0, 1.0]` regardless of model output
4. `rationale` truncated to 240 chars
5. Returns `[]` on any failure — caller falls back automatically to Phase 2 nearest-distance

### `select_anchor()` logic

```
1. Filter scored levels to those within max_distance_pct (8%) of current price
2. If none in band → return None (Phase 2 fallback triggered by caller)
3. From in-band levels meeting min_confidence (0.4) → return highest confidence
4. If none meet min_confidence → return nearest by absolute distance (graceful degradation)
```

Buy orders only consider `type == "support"` levels; sell orders only `type == "resistance"`.

---

## 4. Architecture decisions

### ADR-0009 — LLM Guardrails (new, Accepted)

Three hard rules enforced at the prompt level: (1) only score provided levels, no invented prices; (2) no trade recommendations; (3) no fundamental claims. JSON-schema validation via Pydantic is the runtime safety boundary — LLM output that fails validation is discarded entirely before reaching the suggestion engine. Daily cost cap prevents runaway API spend. Model IDs pinned to specific versions.

### ADR-0010 — Magic-Link Auth (new, Accepted)

HMAC-SHA256 over `"{sid}:{action}:{expires}"`. Action is part of the signed payload so an accept token cannot be replayed as a reject. Single-use enforced by `status == "pending"` check (second click returns 409). `MAGIC_LINK_SECRET` is separate from `ADMIN_TOKEN` and has a different rotation cadence. TTL: 7 days.

### ADR-0006 / ADR-0007 — partial updates (still ⚠ Pending Phase 3c)

Both ADRs received a Phase 3a section documenting the scoring pass and confidence-weighted anchor selection. Final close is deferred to Phase 3c when the full suggestion review pipeline is complete.

---

## 5. `main.py` changes (Phase 3a)

**New in lifespan:**
```python
llm = LLMClient(
    api_key=_settings.anthropic_api_key,
    daily_cost_cap_usd=_settings.llm_daily_cost_cap_usd,
)
app.state.llm = llm
```

**`weekly_fn` partial** updated to bind `llm` as fourth positional argument.

**Two new endpoints:** `PATCH /suggestions/{sid}` (admin-authed, mutates status + stamps `acted_at`) and `GET /suggestions/{sid}/{action}` (HMAC token auth, returns `HTMLResponse`). Both return 409 on already-acted suggestions.

---

## 6. Bugs found and fixed during production validation

### Bug 1 — `max_tokens=1500` truncated every Sonnet response

**Symptom:** All 6 `llm_call_log` rows showed `status = schema_error`, `output_tokens = 1500` (exactly the cap), `error = NULL`.  
**Root cause:** The initial `max_tokens=1500` was too low for a full ticker's S/R level array. Sonnet hit the limit mid-JSON, producing incomplete output that failed Pydantic parsing.  
**Fix:** Raised to `max_tokens=4096`, then `8192` after further truncation. Added a 30% pre-filter on levels before sending to LLM to reduce response size.

### Bug 2 — Sonnet wrapped JSON in markdown fences despite "no preamble" instruction

**Symptom:** After raising `max_tokens`, error changed to `Invalid JSON: expected value at line 1 column 1` with `input_value` starting `'```json\n{'`.  
**Root cause:** Sonnet consistently wrapped its JSON output in ` ```json ... ``` ` fences regardless of the prompt instruction.  
**Fix:** Added `_strip_fences()` in `llm.py` that strips markdown fences and extracts the outermost `{...}` before Pydantic parsing. Also added "no markdown fences" explicitly to the prompt.

### Bug 3 — `llm_call_log.error` logged raw fenced content, hiding the actual parse error

**Symptom:** The `error` column in `llm_call_log` showed the raw fenced string (making the log entry identical to the content), not the actual Pydantic validation error.  
**Root cause:** The diagnostic re-parse in `llm_levels.py` called `model_validate_json(resp.content)` using the raw content rather than the stripped version.  
**Fix:** Changed to `model_validate_json(_strip_fences(resp.content))` so the stored error message reflects what actually failed.

---

## 7. Test coverage

| Test file | Tests | Coverage |
|---|---|---|
| `tests/test_config.py` | 8 | Settings + YAML loader (unchanged) |
| `tests/test_gap.py` | 11 | Gap computation + cash-buffer invariant (Phase 3a pre-flight) |
| `tests/test_load_targets.py` | 5 | Hash-based target dedup (unchanged) |
| `tests/test_email.py` | 3 | FakeEmailer + SMTPEmailer (unchanged) |
| `tests/test_daily_report.py` | 3 | DailyReport + session-close regression (unchanged) |
| `tests/test_indicators.py` | 6 | compute_indicators() (unchanged) |
| `tests/test_levels.py` | 8 | Pivot formulas, swing detection (unchanged) |
| `tests/test_llm.py` | 22 | `_calc_cost` rates + KeyError, cost cap guard, `_spent_today` accumulation, schema validation failure → `None`, day rollover, `_strip_fences` variants |
| `tests/test_magic_link.py` | 12 | Valid roundtrip, wrong action/sid/secret, tampered sig, expired (mocked), malformed token variants |
| `tests/test_suggest.py` | 27 | Phase 2 generation guards + `select_anchor` (highest confidence, nearest fallback, empty list, boundary cases) + `generate_suggestions` scored/fallback paths |
| `tests/test_integration_alpaca.py` | 1 | Full chain vs. live Alpaca (skips without keys — unchanged) |

**Total: 109 unit tests** (up from 58 at Phase 2 close) + 1 integration test.

---

## 8. Known issues and limitations

### LLM scoring adds ~30s to the weekly job per ticker

Each `score_levels_for_ticker()` call is a synchronous Sonnet API call (~25–30s latency). For 6 tickers the weekly job now takes ~3 minutes longer. Acceptable at current scale; Phase 3b or 3c can batch or parallelize if needed.

### Rationale quality varies with market context

Short 60-day OHLCV windows give Sonnet limited context for swing levels that were established earlier. Rationales for older pivot or MA levels are more reliable than those for swing highs/lows from quiet periods.

### No retry on transient API errors

If Anthropic returns a 529 (overloaded) or 5xx, `score_levels_for_ticker()` logs a warning and returns `[]` immediately. The Phase 2 nearest-distance fallback activates, so suggestions still generate — but without LLM scoring. A simple retry with backoff would improve reliability at the cost of latency.

### `prompt_version` is hardcoded to `"v1"` in `score_levels_for_ticker()`

When `score_levels_v1.txt` is replaced by `v2`, the hardcoded string must be updated. A future improvement is to derive the version from the filename or pass it as a parameter.

### Magic-link HMAC secret rotation invalidates all in-flight links

Rotating `MAGIC_LINK_SECRET` immediately invalidates every Accept/Reject link in emails already sent. Rotate only after the current week's suggestions have been acted on or expired (Friday close).

---

## 9. Environment and dependencies

- **New runtime dep:** `anthropic>=0.52` (pinned in `pyproject.toml` from Phase 0 planning — already present, no `uv add` needed)
- **New config keys:** `anthropic_api_key`, `magic_link_secret`, `app_base_url`, `level_prompt_version`, `llm_daily_cost_cap_usd` (all added to `Settings` and `.env.example`)
- **Daily cost cap default:** `$1.00/day` (in both `config.py` and `LLMClient.__init__`)
- **Alembic:** Two new revisions — `40958bbbf005` (`llm_call_log` table) and `000c14406215` (new columns on `sr_level` + `order_suggestion`)
- **Docker:** No Dockerfile changes required

---

## 10. Recommended Phase 3b starting point

Phase 3b introduces LangGraph and the news triage workflow. Based on the Phase 3a foundation:

1. **LangGraph foothold** — install `langgraph`; the `LLMClient` and `HAIKU`/`SONNET` constants from Phase 3a are the natural primitives for graph nodes
2. **News triage graph** — classify (Haiku) → critic (Haiku) → conditional arbitrate (Sonnet); the `llm_call_log` table already supports multi-purpose logging via the `purpose` column
3. **`news_event` table** — new ORM model; daily job fires at 16:30 ET on days a watchlist ticker moved ≥5% vs. prior close
4. **Prompt evolution** — `score_levels_v1.txt` already has the placeholder note: "In a future version you will also receive recent news headlines and earnings context per ticker."
5. **ADR-0011** (news source priority) and **ADR-0012** (LangGraph adoption decision rule) are the two new ADRs for Phase 3b

### Files Phase 3b will primarily touch

| File | Why |
|---|---|
| `models.py` | New `NewsEvent` ORM model |
| `jobs/daily_report.py` | Add movers check; trigger triage graph when threshold met |
| `services/llm.py` | Potentially add streaming support for longer Sonnet responses |
| `prompts/` | New prompt files for classify + critic + arbitrate nodes |
| `migrations/` | New revision for `news_event` table |

---

## 11. Pre-tag checklist

Before tagging `v0.3a.0`:

| # | Item | Status |
|---|---|---|
| 1 | Weekly email received with `(conf X.XX)` and LLM rationale visible in reason column | ⏳ Pending next Sunday (or manual trigger) |
| 2 | `llm_call_log` shows `status = ok` and `output_tokens < 8192` for all tickers | ⏳ Pending next run |
| 3 | `order_suggestion.confidence_at_creation` non-null for new suggestions | ⏳ Pending next run |
| 4 | Accept button click → 200 HTML + `acted_at` populated + second click → 409 | ⏳ Pending |
| 5 | `uv run pytest` — 109 unit tests pass | ✅ Done |
| 6 | `ruff check src/ tests/` — clean | ✅ Done |
| 7 | `mypy src/` — no new errors vs. Phase 2 baseline | ✅ Done |
| 8 | README updated to Phase 3a (new endpoints, new env vars, updated reason format, llm_call_log table) | ✅ Done |
| 9 | ADR-0009 and ADR-0010 written and accepted; ADR-0006/0007 partial updates applied | ✅ Done |
| 10 | CLAUDE.md updated (LLM fallback never-do rule, Phase 3a env vars, HMAC rotation gotcha) | ✅ Done |
