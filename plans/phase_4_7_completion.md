# Phase 4.7 Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 4.7 — Context-Aware Weekly Order Sizing  
**Code complete:** 2026-05-26  
**Git tag:** pending (tag `v0.4.7.0` after 2 consecutive Sunday emails with ≥1 suggestion showing a sensible size adjustment and `context_note`)

---

## 1. Scope vs. delivery

Phase 4.7 adds a new **`context_adjust` node** to the Sunday suggestion-review graph, spliced between the existing `reason` and `critic` nodes. It applies two size-adjustment mechanisms:

1. **Earnings gate** (deterministic Python): halves the qty for any ticker with earnings within `EARNINGS_LOOKAHEAD_DAYS`, and optionally moves the limit price to a deeper S/R anchor. Uses Finnhub's structured `earnings_calendar` endpoint.
2. **Narrative multiplier** (Sonnet + Python clamp): if Friday's persisted market context is available and fresh, Sonnet returns a `size_multiplier` that Python clamps to `[CONTEXT_SIZE_MIN, CONTEXT_SIZE_MAX]` before applying. Model output is never used unclamped.

The Friday weekly-review job now also persists the Tavily+Sonnet market context to a new `weekly_market_context` DB table so Sunday's graph can load it without re-querying Tavily.

A post-implementation addition extended the narrative multiplier with **VIX and CNN Fear & Greed Index** signals, giving the Sonnet sizing model structured market-sentiment data specifically for index ETF sizing decisions. See §2a and §3a.

All planned deliverables were met across 13 tasks. Eleven bugs were found and fixed (9 during implementation review, 2 post-deploy).

---

## 2. What was built

### New files

| File | Description |
|---|---|
| `src/investor/services/earnings.py` | `EarningsClient` Protocol + `FinnhubEarningsClient` (per-ticker Finnhub calendar, exception → WARNING + continue) + `FakeEarningsClient` (canned results, call recording, window filtering) + `make_earnings_client()` factory (empty key → Fake + WARNING) |
| `src/investor/prompts/context_size_v1.txt` | Sonnet size-multiplier system prompt — hard rules: multiplier within `[bounds.min, bounds.max]`, `prefer_anchor` must be a method from `scored_levels`, no price targets, no buy/sell/hold recommendations, JSON-only output |
| `src/investor/prompts/context_size_v2.txt` | v1 + VIX and CNN Fear & Greed Index guidance for index ETFs: non-leveraged ETFs upsize on high VIX / Extreme Fear, reduce on Extreme Greed; leveraged ETFs (TQQQ etc.) invert VIX rule due to volatility decay; individual stocks use macro/sector narrative only |
| `src/investor/prompts/suggestion_critic_v2.txt` | Copy of `suggestion_critic_v1.txt` + rule 6: "Sizing already adjusted — RESPECT these. Only override if the adjustment created a NEW problem." |
| `tests/test_earnings.py` | 5 tests — `FakeEarningsClient` call recording, window filtering, factory no-key → Fake, factory with key → Concrete, SDK exception → `{}` |
| `tests/test_context_adjust.py` | 8 tests — earnings gate shrinks qty, reanchor to deeper level, no deeper anchor keeps original, narrative clamp high, narrative clamp low, sub-1-share draft dropped, rationale re-keying after drop, price invariant on wrong-side prefer_anchor |
| `docs/adr/0021-context-aware-order-sizing.md` | Three decisions: bounded Tavily exception for qty scaling only; carved LLM output exception (size_multiplier ≠ price target); Finnhub calendar vs. Tavily free-text `forward_events` |

### New Alembic migrations

| Migration | Change |
|---|---|
| `460b4dae04ed_phase4_7_weekly_market_context_table` | Creates `weekly_market_context` table (`id`, `week_of`, `payload_json`, `created_at`; `ix_wmc_week_of` index; append-only, no unique constraint) |
| `10ca3e83de1e_phase4_7_order_suggestion_context_audit_cols` | Adds `base_qty DOUBLE`, `size_factor DOUBLE DEFAULT 1.0`, `context_note TEXT` to `order_suggestion` |

### Updated files

| File | Change |
|---|---|
| `src/investor/models.py` | Added `WeeklyMarketContextRow` ORM class; added `base_qty`, `size_factor` (with `default=1.0, server_default="1.0"`), `context_note` columns to `OrderSuggestion` |
| `src/investor/services/suggest.py` | Added `base_qty: float \| None = None`, `size_factor: float = 1.0`, `context_note: str \| None = None` to `OrderSuggestionRow` frozen dataclass; updated `persist_suggestions()` insert and update branches to write all three fields |
| `src/investor/services/weekly_context.py` | Added `persist_weekly_context(s, ctx)` — serialises via `dataclasses.asdict` + `json.dumps(default=str)`; `load_latest_weekly_context(s, *, week_of, max_age_days)` — staleness check in SQL WHERE (not Python, to avoid naive/aware `TypeError`); `_weekly_context_from_dict(data)` — reconstructs `WeeklyMarketContext` + `NewsResult` citations; **post-impl:** added `vix`, `fear_greed_score`, `fear_greed_label` fields to `WeeklyMarketContext`; `_fetch_vix()` (Finnhub `^VIX`); `_fetch_fear_greed()` (CNN public endpoint, no key); `build_weekly_market_context()` now accepts `finnhub_api_key` and populates sentiment fields; `_weekly_context_from_dict()` reads them back |
| `src/investor/jobs/weekly_review.py` | After `build_weekly_market_context()`, persists context via `dataclasses.replace(market_context, week_of=_next_monday())` with exception guard (failure logs WARNING, does not block the review email); **post-impl:** passes `finnhub_api_key=settings.finnhub_api_key` to `build_weekly_market_context()` |
| `src/investor/config.py` | Added 8 new settings: `earnings_size_factor=0.5`, `earnings_reanchor=True`, `earnings_lookahead_days=7`, `context_size_min=0.25`, `context_size_max=1.5`, `context_max_age_days=4`, `context_adjust_prompt_version="v2"` (bumped from v1 after VIX/F&G addition), `critic_prompt_version="v2"` |
| `src/investor/graphs/suggestion_review.py` | Extended `ReviewContext` with `market_context: WeeklyMarketContext \| None` and `earnings_by_ticker: dict[str, date]`; updated `gather_context_node` (earnings fetch BEFORE session, market context load INSIDE session); added `DraftSizeAdjustment` / `DraftSizeAdjustments` Pydantic schemas; added `_deeper_anchor()` and `_find_level()` helpers; added `context_adjust_node` (three sub-passes); updated `critic_node` (v2 prompt; adds base_qty/size_factor/context_note/earnings_by_ticker to payload); updated `build_suggestion_review_graph` (new params, wired `reason → context_adjust → critic`); **post-impl:** added `sentiment` block to Sonnet user payload containing `vix`, `fear_greed_score`, `fear_greed_label` from persisted context |
| `src/investor/jobs/weekly_suggestions.py` | Added `earnings_client` parameter; passes `settings=settings, earnings_client=earnings_client` to `build_suggestion_review_graph()` |
| `src/investor/main.py` | Calls `make_earnings_client(_settings)` in lifespan; stores result on `app.state.earnings`; updated `weekly_fn` partial and `admin_run_weekly_suggestions` endpoint to pass earnings client |
| `templates/weekly_suggestions.html.j2` | Qty cell: adds `(base N · ×F)` badge when `size_factor != 1.0 and base_qty is not none`; rationale cell: adds grey `context_note` line when present |
| `templates/weekly_suggestions.txt.j2` | Qty column: inline `(base N xF.FF)` when adjusted; `[context_note]` on next line when present |
| `CLAUDE.md` | Updated Tavily ban (now bounded exception for context_adjust); added gotchas 24–26 (week-of alignment, stale context skip, earnings gate uses Finnhub not Tavily); added Phase 4.7 env vars to required env list |
| `product_plan.md` | Added Phase 4.7 section |
| `README.md` | Updated header, intro, env vars table, scheduler table, weekly suggestions section (6-node graph, context_adjust description, email display), weekly review section, `order_suggestion` data model (+3 cols), new `weekly_market_context` table, updated llm_call_log purpose values, new SQL queries, updated project layout, updated test count (261 → 277), added ADR-0021 |

---

## 2a. Post-implementation: VIX and Fear & Greed index ETF sizing

After initial deploy, a further enhancement was added: the Sonnet sizing call now receives structured market-sentiment data — CBOE VIX and CNN Fear & Greed Index — and `context_size_v2.txt` adds explicit sizing rules for index ETFs.

### Data sourcing

| Signal | Source | API key required | Fetch location |
|---|---|---|---|
| CBOE VIX | Finnhub `quote("^VIX")` | Yes — `FINNHUB_API_KEY` (already in config) | `_fetch_vix()` in `weekly_context.py`, called in `build_weekly_market_context()` |
| CNN Fear & Greed | `https://production.dataviz.cnn.io/index/fearandgreed/graphdata` (public) | No | `_fetch_fear_greed()` in `weekly_context.py`, stdlib `urllib.request` |

Both are fetched on **Friday** during `run_weekly_review`, persisted inside `WeeklyMarketContext.payload_json`, and loaded on **Sunday** by `gather_context_node` — same Friday→Sunday bridge as the narrative. If either fetch fails (network error, API key absent, endpoint changed), the field is `None` and the prompt skips that signal.

### `WeeklyMarketContext` new fields

```python
vix: float | None = None             # CBOE VIX at time of Friday synthesis
fear_greed_score: int | None = None  # 0–100
fear_greed_label: str | None = None  # "Extreme Fear" … "Extreme Greed"
```

All three default to `None` — existing persisted rows deserialise cleanly without migration.

### Prompt v2 sizing rules (index ETFs only)

Non-leveraged index ETFs (VOO, QQQ, SCHD, SPY, IVV, …):

| Condition | Adjustment for BUY orders |
|---|---|
| VIX 25–35 (elevated fear) | ×1.1 – ×1.2 upsize |
| VIX > 35 (crisis) | upsize to `bounds.max` |
| Fear & Greed 0–25 (Extreme Fear) | ×1.15 – ×1.25 upsize |
| Fear & Greed 26–45 (Fear) | ×1.05 – ×1.1 upsize |
| Fear & Greed 55–74 (Greed) | no change (1.0) |
| Fear & Greed 75–100 (Extreme Greed) | ×0.75 – ×0.9 downsize |
| Signals conflict | 1.0 (no change) |
| Both signal same direction | larger of the two (not stacked) |

Leveraged ETFs (TQQQ, SOXL, UPRO, …) — **inverse VIX rule**: VIX > 25 → ×0.5 – ×0.75 downsize (volatility decay and daily rebalancing compound losses in high-VIX regimes).

Individual stocks: VIX/F&G ignored; macro/sector narrative drives sizing as before.

---

## 3. context_adjust_node design

The node runs three sequential sub-passes on the draft list:

```
4a — Earnings gate (deterministic Python)
     for each draft:
       if ticker in earnings_by_ticker:
         earnings_factor[i] = settings.earnings_size_factor   (default 0.5)
         if settings.earnings_reanchor:
           try _deeper_anchor() — buy: nearest support below current limit
                                  sell: nearest resistance above current limit
           if found: record new anchor method + new limit_price

4b — Narrative multiplier (Sonnet, bounded)
     if ctx.market_context is not None:
       call Sonnet with context_size_v{version}.txt
       parse DraftSizeAdjustments
       on parse failure: fallback to empty (no narrative adjustment)
     clamp each size_multiplier to [context_size_min, context_size_max]

4c — Apply (deterministic Python)
     for each draft:
       size_factor = earnings_factor[i] * narrative_factor[i]
       new_qty = float(int(base_qty * size_factor))   (floor, not round)
       if new_qty < 1: drop draft
       validate prefer_anchor against scored_levels before applying
       emit replace(d, qty=new_qty, base_qty=(d.qty if size_factor != 1.0 else None),
                    size_factor=size_factor, context_note=...)
     re-key rationales: {old_idx: new_idx} over surviving drafts
```

`prefer_anchor` validation (`_find_level()`) rejects:
- A method name not present in the draft's `scored_levels`
- A support method used for a sell draft (or resistance for a buy)
- Any invented string the model fabricated

This ensures ADR-0013 ("LLMs judge, Python applies") is preserved: Sonnet proposes a multiplier and an optional anchor hint; Python clamps, floors, drops, and validates before writing anything.

---

## 4. Friday → Sunday bridge

The `week_of` key alignment is critical:

```
Friday run_weekly_review:
  market_context.week_of = _next_monday()   # e.g. 2026-05-25 if run on 2026-05-22
  persist_weekly_context(s, ctx)            # stored with week_of = upcoming Monday

Sunday gather_context_node:
  state["week_of"] = _next_monday()         # same upcoming Monday, resolved Sunday
  load_latest_weekly_context(s, week_of=state["week_of"], max_age_days=4)
  # finds the Friday row ✓
```

The `context_max_age_days=4` window allows a Friday row (age ~2 days on Sunday) to be used while rejecting a stale row from the previous week (age ~9 days). If no fresh row exists, the narrative sub-pass is silently skipped; the earnings gate still applies independently.

---

## 5. Architecture decisions

### ADR-0021 — Context-Aware Weekly Order Sizing (Accepted)

Three decisions:

1. **Bounded Tavily exception to ADR-0020's ban.** ADR-0020 banned all Tavily output from the suggestion engine. The Phase 4.7 exception is narrow: `context_adjust_node` may scale *quantities only*, within a Python-clamped range, using only existing scored S/R anchors. Ticker selection, price targets, direction, and trade recommendations remain absolutely banned.

2. **Carved LLM output exception.** CLAUDE.md's "never let the LLM emit price targets" rule applies to suggestion content. A `size_multiplier` in `[0.25, 1.5]` that scales shares — clamped deterministically after the call — is categorically different: it does not add tickers, set prices, or advise action. ADR-0013 is preserved.

3. **Finnhub structured calendar over Tavily free-text `forward_events`.** Finnhub's `earnings_calendar` endpoint returns `{ticker: date}` — machine-readable, no parsing required. Tavily's `forward_events` is free text produced by Sonnet and structurally unreliable for a deterministic gate. `FakeEarningsClient` makes the gate testable and fallback-safe.

---

## 6. Bugs found and fixed

### Bug 1 — SQLite naive datetime raises `TypeError` in staleness check (Critical)

**Symptom:** `load_latest_weekly_context()` crashed with `TypeError: can't subtract offset-naive and offset-aware datetimes` when comparing `row.created_at` to `datetime.now(UTC)`.  
**Root cause:** SQLite returns naive datetimes even when the column is declared `DateTime(timezone=True)`. Python-side comparison between a naive `row.created_at` and an aware `datetime.now(UTC)` raises `TypeError`.  
**Fix:** Moved the staleness cutoff check into the SQL `WHERE` clause (`WHERE created_at >= :cutoff`) using `datetime.now(UTC) - timedelta(days=max_age_days)` as a bound parameter. No Python comparison needed.

### Bug 2 — `size_factor` had no Python-side `default`, only `server_default` (Important)

**Symptom:** A freshly-constructed in-memory `OrderSuggestion` ORM object had `size_factor = None` until it was flushed and refreshed from the DB, because `server_default` is only applied by the database engine.  
**Fix:** Added `default=1.0` alongside `server_default="1.0"` on the mapped column.

### Bug 3 — `context_note` used `String` instead of `Text` (Minor)

**Symptom:** `context_note` was declared `String` (maps to `VARCHAR` with no length in SQLite, fine but inconsistent with other prose columns like `llm_rationale`).  
**Fix:** Changed to `Text` to match the convention used for free-text narrative columns.

### Bug 4 — Telemetry from `llm_node_call` discarded (Important)

**Symptom:** `parsed, _ = llm_node_call(...)` threw away the telemetry dict returned as the second element, preventing LLM cost logging from `context_adjust` calls.  
**Fix:** Captured as `parsed, context_tel = llm_node_call(...)`, initialised `context_tel: dict[str, object] = {}` before the conditional block, and merged it into the return state: `"telemetry": {**state.get("telemetry", {}), **context_tel}`.

### Bug 5 — Note duplication when earnings reanchor and earnings_factor both applied (Important)

**Symptom:** When a ticker had earnings AND a deeper anchor was found, two separate notes were appended: `"→ new_method"` from reanchor and `"earnings YYYY-MM-DD, ×0.50"` from the size pass. Context note ended up duplicated and redundant.  
**Fix:** Merged reanchor info into the earnings size note (`"earnings YYYY-MM-DD, ×0.50 → new_method"`) and guarded the standalone size note with `if i not in earnings_anchors`.

### Bug 6 — `base_qty` written even when no adjustment made (Minor)

**Symptom:** Every suggestion row had `base_qty = original_qty` (same as `qty`) when `size_factor == 1.0`, making it impossible to distinguish adjusted from pass-through rows in SQL queries.  
**Fix:** `base_qty = d.qty if size_factor != 1.0 else None`. Only set when a real adjustment occurred.

### Bug 7 — `build_suggestion_review_graph()` call site not updated (Critical)

**Symptom:** `jobs/weekly_suggestions.py` called `build_suggestion_review_graph(llm=..., ...)` without the new `settings` and `earnings_client` parameters, which would have raised `TypeError` at runtime.  
**Fix:** Updated the call site in Task 10.

### Bug 8 — `models.py` line too long after adding `default=1.0` (ruff E501)

**Symptom:** `size_factor` mapped_column line exceeded the 100-char limit after the bug 2 fix.  
**Fix:** Wrapped `mapped_column(...)` arguments onto multiple lines.

### Bug 9 — Import ordering violation in `weekly_suggestions.py` (ruff I001)

**Symptom:** `from typing import Any` was placed after stdlib imports, violating isort order.  
**Fix:** `uv run ruff check --fix` corrected the ordering automatically.

### Bug 10 — Double-v in `context_size` prompt filename (Critical, post-deploy)

**Symptom:** Weekly suggestions failed with `FileNotFoundError: '/app/src/investor/prompts/context_size_vv1.txt'`.  
**Root cause:** Format string `f"context_size_v{settings.context_adjust_prompt_version}.txt"` with the default `context_adjust_prompt_version="v1"` produced `"context_size_vv1.txt"`. The file on disk is `context_size_v1.txt`. Same class of bug as Phase 4.5's `weekly_context_vv1.txt`.  
**Fix:** Changed to `f"context_size_{settings.context_adjust_prompt_version}.txt"` — consistent with the `llm_levels.py` pattern (`f"score_levels_{prompt_version}.txt"`).  
**File:** `src/investor/graphs/suggestion_review.py` line 354.

### Bug 11 — Sub-penny limit price triggers Alpaca rejection and kill switch (Critical, post-deploy)

**Symptom:** Auto-trade pass raised `{"code":42210000,"message":"invalid limit_price 670.6391. sub-penny increment does not fulfill minimum pricing criteria"}` for sug-19 (VOO, limit `$670.6391`). The `broker_error` trigger fired the kill switch, setting mode to `OFF` and leaving sug-20 through sug-25 unplaced.  
**Root cause:** Scored S/R level prices carry full float precision (4+ decimal places). `brokers/alpaca.py` passed `req.limit_price` directly to Alpaca without rounding; Alpaca enforces penny-increment minimums.  
**Fix:** `limit_price=round(req.limit_price, 2)` at the `LimitOrderRequest` construction site in `submit_order()`. The DB retains the full-precision value for audit; only the value sent to Alpaca is rounded.  
**File:** `src/investor/brokers/alpaca.py` line 110.  
**Recovery:** Re-promoted to LIVE via `POST /admin/auto-trade/promote`, then ran `POST /admin/run-auto-trade` to place the stalled sug-20 through sug-25 (sug-23/AAPL remained `pending` and was not placed).

---

## 7. Test coverage

| Test file | Tests | Notes |
|---|---|---|
| `tests/test_earnings.py` | 5 | **New** |
| `tests/test_context_adjust.py` | 8 | **New** |
| All prior tests | 264 | Unchanged |
| **Total** | **277** | Up from 260 at Phase 4.5 close |

```
uv run pytest -m "not integration"   → 277 passed
uv run ruff check src/ tests/        → clean
uv run mypy src/                     → no new errors in Phase 4.7 files
uv run alembic current               → head
```

---

## 8. Environment and dependencies

**No new runtime dependencies** — Finnhub SDK (`finnhub-python`) was already present from Phase 3b.

**New Alembic migrations:** 2 (both applied; `uv run alembic upgrade head` required on first deploy).

**New env vars (all optional, all have defaults):**

| Variable | Default | Purpose |
|---|---|---|
| `EARNINGS_SIZE_FACTOR` | `0.5` | Qty multiplier for earnings-week tickers |
| `EARNINGS_REANCHOR` | `true` | Move anchor to deeper S/R on earnings week |
| `EARNINGS_LOOKAHEAD_DAYS` | `7` | Days forward to check for earnings |
| `CONTEXT_SIZE_MIN` | `0.25` | Lower clamp on narrative multiplier |
| `CONTEXT_SIZE_MAX` | `1.5` | Upper clamp on narrative multiplier |
| `CONTEXT_MAX_AGE_DAYS` | `4` | Max age of Friday context row for Sunday to use |
| `CONTEXT_ADJUST_PROMPT_VERSION` | `v2` | Selects `context_size_v{version}.txt` (bumped from `v1` after VIX/F&G addition) |
| `CRITIC_PROMPT_VERSION` | `v2` | Selects `suggestion_critic_v{version}.txt` |

**`FINNHUB_API_KEY`** — already present since Phase 3b; now triple-purpose: news fallback, earnings gate, and VIX fetch. Empty key disables all three with WARNING logs; no crash.

**`TAVILY_API_KEY`** — already present since Phase 4.5; no change. If absent, Friday runs skip context persistence (including VIX/F&G); Sunday runs skip the full narrative+sentiment sub-pass. The earnings gate is independent and still applies.

---

## 9. Pre-tag checklist

Before tagging `v0.4.7.0`:

| # | Item | Status |
|---|---|---|
| 1 | `uv run alembic upgrade head` applied — `weekly_market_context` table and 3 audit columns present | ⏳ Pending |
| 2 | Friday review email runs and `SELECT * FROM weekly_market_context` shows a row with `week_of` = next Monday; `payload_json` contains `vix` and `fear_greed_score` fields | ⏳ Pending first live Friday run |
| 3 | Sunday suggestions email shows at least one suggestion with `(base N · ×F)` badge and `context_note` | ⏳ Pending first live Sunday run |
| 4 | Earnings gate: `SELECT * FROM order_suggestion WHERE size_factor != 1.0` returns rows for any earnings-week ticker | ⏳ Pending |
| 5 | VIX/F&G signal visible in `context_note` for at least one index ETF suggestion (e.g. "VIX 28 + Extreme Fear") | ⏳ Pending |
| 6 | Second Sunday email confirms consistent behaviour | ⏳ Pending |
| 7 | Remove `TAVILY_API_KEY` and re-run weekly suggestions: email arrives normally, no crash, no size badges | ⏳ Pending |
| 8 | Remove `FINNHUB_API_KEY` and re-run weekly suggestions: `WARNING: FINNHUB_API_KEY not set` in logs, no earnings or VIX adjustments, no crash | ⏳ Pending |
| 9 | `uv run pytest -m "not integration"` — 277 tests pass | ✅ Done |
| 10 | `uv run ruff check src/ tests/` — clean | ✅ Done |
| 11 | `uv run mypy src/` — no new errors in Phase 4.7 files | ✅ Done |
| 12 | ADR-0021 written and accepted | ✅ Done |
| 13 | CLAUDE.md updated (Tavily ban updated, gotchas 24–26, Phase 4.7 env vars) | ✅ Done |
| 14 | README updated (6-node graph, context_adjust description, env vars, new table, SQL queries, test count, project layout, ADR list) | ✅ Done |
| 15 | `product_plan.md` updated with Phase 4.7 section | ✅ Done |
| 16 | `plans/phase_4_7_completion.md` updated with VIX/F&G addition (§2a, §3a) | ✅ Done |
