# Phase 4.5 Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 4.5 — Tavily Weekly Market Context  
**Code complete:** 2026-05-20  
**Git tag:** pending (tag `v0.4.5.0` after 2 consecutive Friday review emails with non-empty Weekly Market Context content)

---

## 1. Scope vs. delivery

Phase 4.5 added an 8th section to the Friday weekly review email: **Weekly Market Context**. It is powered by Tavily search API (fanout queries) + Claude Sonnet synthesis, and is strictly informational — no Tavily output flows into the suggestion engine or order-execution path.

All planned deliverables were met. Two bugs were found during implementation testing, one post-deploy config error was fixed, and two pre-existing issues (movers Monday lookback, promote endpoint datetime comparison) were corrected during the session.

---

## 2. What was built

### New services

| File | Description |
|---|---|
| `src/investor/services/tavily.py` | `TavilyClient` Protocol + `TavilyConcreteClient` (monthly cap enforcement, SDK exception handling, lazy import) + `FakeTavilyClient` (test double, canned results by query) + `make_tavily_client()` factory |
| `src/investor/services/weekly_context.py` | `build_weekly_market_context()` — ~12–16 Tavily fanout queries + Sonnet synthesis; `WeeklyMarketContext` frozen dataclass; `_infer_sectors()` ticker→sector map; graceful `None` return when all results empty |

### New prompt

| File | Description |
|---|---|
| `src/investor/prompts/weekly_context_v1.txt` | Sonnet system prompt — hard rules: no price targets, no buy/sell/hold recommendations, no claims unsupported by Tavily input, no invented entities. JSON output: `macro_summary`, `sector_summary`, `ticker_catchup`, `forward_events` |

### Updated files

| File | Change |
|---|---|
| `src/investor/config.py` | Added `tavily_api_key`, `tavily_monthly_cap` (default 200), `weekly_context_prompt_version` (default `"v1"`); changed `opend_host` default from `"host.docker.internal"` to `""` |
| `src/investor/jobs/weekly_review.py` | Added `tavily: TavilyClient` parameter; added `market_context: WeeklyMarketContext \| None = None` field to `WeeklyReview`; added `build_weekly_market_context()` call block with try/except degradation |
| `src/investor/main.py` | Wired `make_tavily_client()` in lifespan; updated `weekly_review_fn` partial; updated admin endpoint; fixed `("alpaca_paper", "LIVE")` soak window to 0 days; fixed offset-naive datetime comparison in soak check |
| `templates/weekly_review.html.j2` | New Section 7 "Weekly Market Context" between auto-trade and Moomoo, guarded by `{% if review.market_context %}` — fully absent when `None` |
| `templates/weekly_review.txt.j2` | Plain-text mirror of Section 7 |
| `.env.example` | Added `OPEND_HOST`, `OPEND_PORT`, `OPEND_SECURITY_FIRM`, `AUTO_TRADE_PROMOTION_TOKEN`, `TAVILY_API_KEY`, `TAVILY_MONTHLY_CAP`; removed stale "moomoo not yet implemented" comment |

### New tests

| File | Tests | Description |
|---|---|---|
| `tests/test_tavily.py` | 11 | `FakeTavilyClient` call recording + canned returns; factory (no-key → Fake, key → Concrete); monthly cap enforcement; SDK exception handling; result mapping; content truncation |
| `tests/test_weekly_context.py` | 5 | Happy path; all-empty → `None`; LLM parse failure → citations-only; URL dedup; citations capped at 15 |

### New ADR

| File | Decision |
|---|---|
| `docs/adr/0020-tavily-weekly-context.md` | Why Tavily (finance/news topics, `days` filter, LLM-optimised content); Protocol swap path to Serper/Brave/Perplexity; Nebius acquisition risk + SDK pin rationale; informational-only hard constraint |

---

## 3. Fanout query design

```
Macro (2 queries)
  ├── search_news("US Federal Reserve policy this week",   days=7, max=4)
  └── search_finance("US equity market this week",        days=7, max=3)

Sectors (1 query per unique sector in watchlist)
  └── search_finance("{sector} sector news this week",   days=7, max=3)

Per-ticker catch-up (1 query per ticker)
  └── search_finance("{ticker} stock news this week",    days=7, max=3)

Forward-looking (2 queries)
  ├── search_news("US stock market earnings calendar next week", days=2, max=5)
  └── search_news("US Federal Reserve next week schedule",      days=2, max=3)

Total: ~12–16 searches per Friday run  (~60/month, well within free-tier 1,000 cap)
```

All results are collected, deduplicated by URL, sorted by Tavily relevance score descending, and capped at 15 citations. These are passed to Sonnet alongside per-category snippets for synthesis.

---

## 4. Graceful degradation chain

```
TAVILY_API_KEY not set
  → make_tavily_client() returns FakeTavilyClient (all queries → [])
  → build_weekly_market_context() returns None
  → WeeklyReview.market_context = None
  → {% if review.market_context %} → section absent from email ✓

Monthly cap reached (_used_this_month >= _cap)
  → all searches return []  +  WARNING log
  → same None path above ✓

SDK exception on individual query
  → that query returns []  +  WARNING log  +  exc_info=True
  → other queries continue normally ✓

All Tavily results empty (no cap, no exception)
  → build_weekly_market_context() returns None
  → section absent from email ✓

LLM schema parse failure (parsed is None)
  → WeeklyMarketContext(macro_summary="", sector_summary="", ticker_catchup={},
                        forward_events=[], citations=<collected results>)
  → email shows source citations only — still useful for manual review ✓
```

The Friday review email is never blocked by a Tavily outage.

---

## 5. Architecture decisions

### ADR-0020 — Tavily Weekly Market Context (Accepted)

Key decisions:
- **Protocol over ABC** — `TavilyClient` is a `typing.Protocol`; swap requires only a new concrete class + factory change, no call-site changes
- **SDK pinned `>=0.6,<0.7`** — Tavily acquired by Nebius Feb 2026; pre-1.0 API surface has churn risk
- **Monthly cap is per-instance** — resets on process restart; ~60 searches/month at weekly cadence is well under the 200 default; acceptable for personal use
- **Informational only** — `WeeklyMarketContext` is a frozen dataclass with no code path to `generate_suggestions()`, `run_auto_trade_pass()`, or any broker adapter; enforced in CLAUDE.md "Things to never do" and ADR-0020

---

## 6. Bugs found and fixed

### Bug 1 — `_domain()` strips leading characters instead of "www." prefix

**Symptom:** `_domain("https://wsj.com/article")` returned `"sj.com"` instead of `"wsj.com"`.  
**Root cause:** `str.lstrip("www.")` strips all leading characters in the set `{'w', '.'}`, not the literal prefix `"www."`. For `wsj.com`, the leading `w` was consumed.  
**Fix:** Replaced `netloc.lstrip("www.")` with `netloc.removeprefix("www.")`.  
**Detected by:** `test_maps_result_to_news_result_fields`.

### Bug 2 — Prompt filename doubled the "v" prefix

**Symptom:** `build_weekly_market_context()` raised `FileNotFoundError: weekly_context_vv1.txt`.  
**Root cause:** Format string `f"weekly_context_v{prompt_version}.txt"` with `prompt_version="v1"` (the config default) produced `"weekly_context_vv1.txt"`. The file on disk is `weekly_context_v1.txt`.  
**Fix:** Changed format string to `f"weekly_context_{prompt_version}.txt"` — consistent with the `llm_levels.py` pattern (`f"score_levels_{prompt_version}.txt"`).  
**Detected by:** `test_happy_path_returns_populated_context`.

### Bug 3 (post-deploy) — `opend_host` default causes false "parallel running" Moomoo status

**Symptom:** Weekly review email showed "Moomoo OpenD is running in PARALLEL" on every install, even with no Moomoo configuration.  
**Root cause:** `opend_host` defaulted to `"host.docker.internal"` (non-empty string), making `elif settings.opend_host:` always truthy.  
**Fix:** Changed default to `""`. `OPEND_HOST` must now be explicitly set in `.env` to trigger parallel-running status.

### Bug 4 (post-deploy) — Promote endpoint `TypeError` on soak window check

**Symptom:** `POST /admin/auto-trade/promote` returned 500 with `TypeError: can't subtract offset-naive and offset-aware datetimes`.  
**Root cause:** `last_entry.ts` from SQLite is stored as a naive datetime; `datetime.now(UTC)` is timezone-aware. Subtraction fails.  
**Fix:** Added `.replace(tzinfo=UTC)` to `last_entry.ts` before the comparison.

### Improvement — `("alpaca_paper", "LIVE")` soak window reduced to 0

The original 14-day DRY_RUN soak before paper LIVE was overcautious — paper trading has no real money at stake. The meaningful gate is `alpaca_live` (28 days paper LIVE). Changed to 0 so OFF → DRY_RUN → LIVE on `alpaca_paper` is immediate.

---

## 7. Additional fixes during session

### Movers Monday lookback widened to 48 hours

**Context:** MU received a "no material news" movers email on Monday despite moving ≥5% vs. last week.  
**Root cause:** The news lookback was a fixed 24h. Weekend and Friday news falls outside that window by Monday market open.  
**Fix:** `jobs/movers.py` now uses 48h on Mondays (`datetime.weekday() == 0`), 24h otherwise. URL-hash dedup in `services/news.py` prevents double-insertion.  
**Commit:** `9acb99c`

---

## 8. Test coverage

| Test file | Tests | Notes |
|---|---|---|
| `tests/test_tavily.py` | 11 | **New** |
| `tests/test_weekly_context.py` | 5 | **New** |
| All prior tests | 244 | Unchanged |
| **Total** | **260** | Up from 240 at Phase 4 close |

```
uv run pytest -m "not integration"   → 260 passed
uv run ruff check src/ tests/        → clean
uv run mypy src/                     → 0 new errors in Phase 4.5 files
```

---

## 9. Environment and dependencies

**New runtime dependency:** `tavily-python>=0.6,<0.7` (added to `pyproject.toml`)

**New env vars:**

| Variable | Default | Required |
|---|---|---|
| `TAVILY_API_KEY` | `""` | No — empty = section skipped |
| `TAVILY_MONTHLY_CAP` | `200` | No |
| `WEEKLY_CONTEXT_PROMPT_VERSION` | `"v1"` | No |

**Changed default:**

| Variable | Was | Now |
|---|---|---|
| `OPEND_HOST` | `"host.docker.internal"` | `""` |

**No Alembic migrations** — no schema changes.  
**No Docker changes** — `tavily-python` is installed via `uv sync` inside the existing image build.

---

## 10. Pre-tag checklist

Before tagging `v0.4.5.0`:

| # | Item | Status |
|---|---|---|
| 1 | `TAVILY_API_KEY` added to `.env` | ⏳ Pending |
| 2 | Friday review email received with non-empty "Weekly Market Context" section | ⏳ Pending first live Friday run |
| 3 | Macro summary, sector summary, ticker catch-up, and forward events all populated | ⏳ Pending |
| 4 | Source citations appear with working URLs | ⏳ Pending |
| 5 | Second Friday email confirms consistent results | ⏳ Pending |
| 6 | Remove `TAVILY_API_KEY` and re-trigger: email arrives without the section (no broken HTML) | ⏳ Pending |
| 7 | `uv run pytest -m "not integration"` — 260 tests pass | ✅ Done |
| 8 | `ruff check src/ tests/` — clean | ✅ Done |
| 9 | `mypy src/` — no new errors in Phase 4.5 files | ✅ Done |
| 10 | ADR-0020 written and accepted | ✅ Done |
| 11 | CLAUDE.md updated (repo layout, never-do, gotchas 21–23, Phase 4.5 env vars) | ✅ Done |
| 12 | README updated (8-section, env vars, ADR list, test count, project layout) | ✅ Done |
| 13 | `.env.example` updated with all Phase 4 and 4.5 vars | ✅ Done |
| 14 | Moomoo status shows "unavailable" in email (not "parallel running") | ✅ Done — opend_host default fixed |
| 15 | Auto-trade mode is `OFF` in DB | ✅ Confirmed |
