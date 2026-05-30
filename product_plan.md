# Long-Term Investor Assistant — Product Plan (v1)

**Owner:** Jane · **Date:** 2026-04-24 (last update 2026-05-18) · **Stage:** Phase 4 code-complete — pending first Friday review email to tag `v0.4.0-phase-4-code-complete`. Moomoo primary-flip is a separate manual decision post-soak.

---

## 1. Product in one paragraph

A self-hosted, always-on assistant for a long-term investor. The user declares a watchlist and a target allocation. The system pulls positions and prices daily, computes the gap between current and target, identifies support/resistance levels on each ticker, and emails a weekly order suggestion plus daily news/price alerts (anything ±5 %, ±10 % vs. last week's close). It also surfaces **untracked positions** — anything held in the broker without a matching entry in `targets.yaml` — as a deliberate red banner so paper trades, legacy holdings, or accidental buys can never silently distort the gap math. **By default, the user places orders themselves** — the product is suggest-only. From Phase 4.6 onward, there is an optional opt-in **auto-trade mode** (off by default, gated behind a three-state switch with admin access, hard caps, and a kill switch) that places already-accepted suggestions through the broker API. Designed for one user (Jane) first, with a clean path to becoming a multi-tenant product later — auto-trade stays single-user-only forever; multi-tenant auto-trade would cross into regulated advice and is explicitly out of scope.

---

## 2. Tool choice — Moomoo OpenD vs. alternatives

Your constraints (**US equities + ETFs only, suggest-only, self-hosted**) change the calculus. OpenD shines on HK/SG where alternatives are weak; for US-only it adds friction (a local gateway process, session auth, Windows/macOS-leaning support) without a real benefit.

### Brokerage / portfolio API

| Option | Fit for your case | Notes |
|---|---|---|
| **Alpaca** ⭐ recommended | Excellent | Pure REST + WebSocket, no gateway, free paper trading, fractional shares, commission-free, great for US equities/ETFs. Same API for paper and live. **Available to Australian tax residents** for live trading; paper is open to anyone. |
| Interactive Brokers (IBKR) | Good if you want global later | Requires IB Gateway (similar to OpenD). Most mature. Steeper learning curve. IBKR Australia is fully licensed. |
| Schwab API | OK if you already bank at Schwab | Official post-TDA. OAuth flow is painful, docs inconsistent. Not available to AU residents. |
| Tradier | Good developer API | $10/mo brokerage plan, free sandbox. US-only sign-up. |
| **Moomoo OpenD** | Over-engineered for US-only | Requires local gateway process. Moomoo AU is licensed and supports US equities from Australia. Keep as your "later" option for live trading — the API semantics differ meaningfully from Alpaca. |

**Recommendation:** Build against **Alpaca** (paper first, then live). Keep the broker layer behind an interface so you can swap to Moomoo/IBKR later without rewriting the product.

> **Australia notes:** Alpaca opens live accounts for AU tax residents; funding is via **Rapyd** (AUD → USD, USD-only balance at Alpaca). Moomoo AU is also an option and many AU long-term investors already hold US equities there — that's why keeping a clean broker interface matters: you can start on Alpaca paper this weekend, run Alpaca live with small test capital, and later route real long-term capital through Moomoo without touching the product logic.

### Market data (prices, bars, indicators)

| Option | Cost | Why |
|---|---|---|
| **Alpaca market data** (free IEX feed) | Free with account | Good enough for EOD + intraday bars for long-term investing. |
| Polygon.io | $29+/mo | Best quality, consolidated SIP tape, institutional-grade. |
| yfinance (Yahoo, unofficial) | Free | Fine for backfill and prototyping; do not rely on in prod. |

**Recommendation:** Alpaca feed for live; yfinance for historical backfill.

### News

| Option | Cost | Why |
|---|---|---|
| **Alpaca News API** (Benzinga-powered) | Free with account | Ticker-tagged, real-time, easiest wire-up. |
| Finnhub | Free tier (60 req/min) | Good company news + basic sentiment; useful as a second source. |
| Polygon News | Paid | Higher quality, deduped. |

**Recommendation:** Alpaca News primary, Finnhub as backup/cross-check.

### LLM for news triage

| Option | Why |
|---|---|
| **Claude Haiku 4.5** for bulk triage, **Claude Sonnet 4.6** for deep summary | Cheap + fast for "is this material?" routing, escalate the 10 % that matter. Typical daily cost: well under $0.50. |
| GPT-4/5 | Comparable, pick on preference. |
| Local (Ollama + Llama 3) | Free but weaker on financial nuance. |

### Scheduler / runtime

- **APScheduler** (in-process cron) is enough for Phases 1–4.
- Move to Celery Beat + Redis only if you add interactive webhooks or multi-user load.

### Database — three-tier split (decided in Phase 0; see ADR-0002)

The original recommendation here was "DuckDB as the single store." Phase 0's implementation evaluated that against the maturity of `duckdb-engine` + Alembic and landed on a different split that's now the architecture of record:

| Tier | Engine | Where it lives | What it owns |
|---|---|---|---|
| **OLTP** | SQLite via SQLAlchemy + Alembic | `data/investor.db` | `target_allocation`, `broker_account`, `positions_snapshot`, `meta`, `alembic_version` — everything transactional |
| **Analytics** | DuckDB as a Python-level engine — **no DB file** | in-memory connection per query | `price_bar` view over Parquet bars; window functions for indicators |
| **Bars** | Parquet | `data/bars/<TICKER>.parquet` | One file per ticker; written by `backfill_bars.py`; read by DuckDB |

Why this beat the DuckDB-everywhere plan:

- **Alembic + SQLite is bulletproof** — `--autogenerate` works, batch mode handles SQLite's missing ALTER COLUMN, the migration story is the same one a million projects already use. `duckdb-engine` + Alembic was workable but accumulated friction.
- **The analytical workload doesn't need a DB file.** DuckDB's killer feature for this app is `read_parquet('data/bars/*.parquet')` — a fresh in-memory connection costs microseconds, has no locks, and can be opened from anywhere in the codebase without touching the SQLAlchemy session. Treating DuckDB as a per-query engine rather than a persistent store removes a whole class of concerns (lock contention, schema migrations on the analytics tier, single-writer constraints).
- **OLTP and analytics never compete for a write lock** because they touch different files. `backfill_bars.py` can run while the FastAPI server is up; under the DuckDB-everywhere design that would have contended.

| | SQLite (OLTP) | DuckDB (analytics, in-memory) | Postgres (Phase 5+) |
|---|---|---|---|
| Analytics on OHLCV bars (window fns, aggregations) | ⭐ row-based | ⭐⭐⭐ vectorized columnar | ⭐⭐ |
| Small OLTP (inserting a suggestion, an alert) | ⭐⭐⭐ best | n/a | ⭐⭐⭐ |
| Read Parquet/CSV directly | ✗ | ⭐⭐⭐ native | extension |
| Concurrent writers | serialised | n/a (read-only, in-memory) | many |
| SQLAlchemy/Alembic maturity | excellent | n/a (we don't use it through SQLAlchemy) | excellent |

**Migration path at Phase 5:** swap the OLTP tier from SQLite to Postgres (a one-day SQLAlchemy + Alembic job because the abstraction is already in place). Analytics tier stays as-is, or moves to MotherDuck if the bars dataset grows large or needs to be shared across processes.

### Notifications

- **SMTP via Gmail App Password** (free, 5 min setup) for v1.
- Migrate to **SendGrid / Resend** if volume grows or you need templates.

---

## 3. Architecture

```
┌───────────────────────────────────────────────────────────┐
│     Runtime: Mac (local dev) OR Docker Compose host       │
│     (same image; `docker compose up` on either)           │
│                                                           │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────┐    │
│  │  Scheduler  │──▶│   Core App   │──▶│   Emailer    │    │
│  │ (APScheduler│   │  (FastAPI +  │   │ (SMTP/Resend)│    │
│  │   jobs)     │   │   services)  │   └──────────────┘    │
│  └─────────────┘   └──────┬───────┘                       │
│                           │                               │
│         ┌──────────┬──────┴──────┬───────────────┐        │
│         ▼          ▼             ▼               ▼        │
│  ┌──────────┐ ┌──────────┐ ┌─────────────┐ ┌───────────┐  │
│  │ SQLite   │ │ DuckDB   │ │  LLM client │ │BrokerAdptr│  │
│  │ (OLTP,   │ │ (in-mem, │ │ (Claude API)│ │ interface │  │
│  │ Alembic) │ │ Parquet) │ └─────────────┘ └─────┬─────┘  │
│  └──────────┘ └────┬─────┘                       │        │
│  data/investor.db  │                             │        │
│              data/bars/*.parquet                 │        │
└──────────────────────────────────────────────────│────────┘
                                              ▼
              ┌─────────────────────────────────────────┐
              │  Alpaca (v1 — paper, then live)         │
              │  Moomoo OpenD   (later, via subprocess) │
              │  IBKR Gateway   (optional later)        │
              └─────────────────────────────────────────┘
```

Key principles:

- **One `BrokerAdapter` interface** (`get_positions`, `get_account`, `get_bars`, `submit_order_draft`, `get_activities`). Alpaca is v1 implementation; Moomoo and IBKR slot in behind the same contract.
- **Same artifact on Mac and Docker.** One Dockerfile. On Mac you can either `uv run` directly, or run the same image via `docker compose up`. DB and secrets live in a bind-mounted `./data` and `./.env` so the file persists across runs and is identical in both modes.
- **Broker selection via env var.** `BROKER=alpaca_paper` / `BROKER=alpaca_live` / `BROKER=moomoo` chooses the adapter at startup. No code changes to migrate.
- **Moomoo caveat for Mac + Docker:** OpenD is a separate Moomoo-supplied process and is most stable running on the host Mac (not inside the app container). The app talks to it via `host.docker.internal:11111`. Keep it this way — don't try to cram OpenD into your Docker image.

---

## 4. Data model (minimal)

```
watchlist            (id, ticker, asset_class, notes, added_at)
target_allocation    (id, ticker, target_pct, band_low_pct, band_high_pct, effective_from, effective_to)
target_change_event  (id, ts, trigger [manual|funds_added|quarterly|annual],
                      previous_targets_json, new_targets_json, reason, actor)
funds_event          (id, ts, delta_usd, source [deposit|withdraw|dividend|fee], detected_from)
positions_snapshot   (id, ts, ticker, qty, avg_cost, market_value, weight_pct)
price_bar            (ticker, date, open, high, low, close, volume)   -- also stored as Parquet
sr_level             (id, ticker, type [support|resistance], price, method, as_of)
order_suggestion     (id, week_of, ticker, side, qty, limit_price, reason,
                      status [pending|accepted|rejected|expired], targets_version)
order_execution      (id, suggestion_id, filled_at, filled_qty, filled_price, source)
alert                (id, ts, ticker, kind [price|news|review|funds], payload_json, sent_at)
news_event           (id, ts, ticker, headline, url, llm_summary, llm_sentiment, llm_material)
broker_account       (id, broker [alpaca|moomoo|ibkr], mode [paper|live], cash_usd, equity_usd, last_sync)
```

Why `target_allocation` is time-versioned (`effective_from`/`effective_to`) and why `target_change_event` exists: you'll want to answer questions like "was my portfolio on target for the targets I had *at the time*, not today's targets?" That matters for honest review of your own decisions.

---

## 5. Tech stack

- **Language:** Python 3.12
- **Framework:** FastAPI (API + health checks) + APScheduler (cron)
- **DB:** three-tier — **SQLite** for OLTP (`data/investor.db`, via SQLAlchemy + Alembic) + **DuckDB** as an in-memory analytical engine (no DB file) + **Parquet** for bars (`data/bars/*.parquet`). Phase 5 swaps SQLite → Postgres; analytics tier stays put. See ADR-0002.
- **Indicators:** `pandas-ta` (easier than TA-Lib to install) + SQL window functions in DuckDB
- **Broker:** `alpaca-py` (v1); `moomoo-api` (later, with OpenD running on host Mac)
- **LLM:** `anthropic` SDK (Haiku 4.5 for triage, Sonnet 4.6 for review summaries)
- **Email:** `smtplib` + Jinja2 templates (Gmail App Password to start)
- **Container:** single Dockerfile. Runs identically on your Mac (`docker compose up`) or a VPS. Mac-native dev via `uv run` uses the same code.
- **Volumes:** `./data` (DuckDB file + Parquet bars) and `./.env` bind-mounted into the container.
- **Secrets:** `.env` file `chmod 600`; later swap to Doppler/1Password Connect.

---

## 6. Phased build plan

Each phase ships something useful on its own. Total MVP: roughly **6–10 weeks of evenings**, plus a 4–6 week paper-trading soak before real money.

### Phase 0 — Foundation ✅ Complete (2026-04-28, tag `v0.0.1-phase-0`)
- Alpaca paper account live; API keys in `.env`.
- `targets.yaml` loaded: VOO 40 / QQQ 25 / SCHD 15 / AMZN 5 / AAPL 5 / MSFT 5 / cash 5.
- Python 3.13 actually shipped (CLAUDE.md says 3.12 — needs reconciling).
- FastAPI + APScheduler (one-shot `DateTrigger` 30 s after start) + DuckDB + Alpaca read-only adapter.
- `/health`, `/positions`, `/gap`, `/admin/run-sync` endpoints; `scripts/sync_positions.py`, `scripts/load_targets.py`.
- All inline SQL extracted to `src/investor/sql/*.sql` (architectural improvement beyond plan).
- `broker_account` time-versioned and `account_id` propagated everywhere (improvement beyond plan).
- 16 unit tests passing; no integration tests yet.

**Phase 0 deviations and follow-ups (carried into Phase 1):**
- **Alembic was skipped.** Schema changes are handled inline via `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in `init_db()`. This needs a real ADR-0002 in Phase 1, ideally adopting Alembic before schema churn picks up.
- **`load_targets.py` has an intermittent dedup bug** — duplicate `target_allocation` rows have appeared. Hardened to `round(float(x), 6)` but not fully tested. Phase 1 must add a regression test and stop running this on every container start.
- **`ALPACA_BASE_URL` is stored but ignored** — `alpaca-py` uses `paper=True` instead. Either remove the env var or wire it; don't leave it as a footgun.
- **Python version drift** — actual runtime is 3.13, docs say 3.12. Pick one, update everywhere.

### Phase 1 — Portfolio & gap ⚙️ Code complete (2026-05-04) + pre-tag cleanup (2026-05-05); tag `v0.1.0-phase-1` deferred until 5-day email streak observed (earliest 2026-05-09)
- Recurring daily sync (`CronTrigger`, Mon–Fri 16:15 ET) ✓
- Daily portfolio email: positions, gap, drift alerts, top-3 over/under summary, HTML + plain-text MIME ✓
- `band_status` (under/in/over) wired into `/gap` and surfaced as `/drift` ✓
- 2-year OHLCV backfill (`scripts/backfill_bars.py`) and update script (`scripts/update_bars.py`, not yet wired into scheduler) ✓
- DuckDB-on-Parquet analytics layer in `services/analytics.py` ✓
- `AccountSnapshot` frozen dataclass + verified `positions` returned as session-safe SQL Row tuples ✓
- 29 unit tests (incl. session-close regression) + 1 integration test against Alpaca paper ✓
- Two bugs caught and fixed during deployment: templates dir absent from Docker image; detached SQLAlchemy instance in `compose_daily_report` ✓
- Pre-tag cleanup pass on 2026-05-05 closed six of seven flagged carryovers (see below).

**Phase 1 cleanup status (post-2026-05-05):**

| Carryover | Status | Resolution |
|---|---|---|
| ADR-0002 (storage architecture) | ✅ Closed | Rewrote `docs/adr/0002-*.md` as "Three-Tier Storage Architecture" |
| ADR-0003 (schema migrations) | ✅ Closed | Rewrote `docs/adr/0003-*.md` as "Schema Migrations with Alembic + SQLite" |
| Bug 2 regression test | ✅ Closed | `test_account_snapshot_survives_session_close` added |
| `AccountSnapshot` pattern generalized | ✅ Closed | Verified — positions are SQL Row tuples (already session-safe); annotation comment clarified |
| Cash-buffer scaling | ✅ Closed | Evaluated; no bug exists (targets sum to 95 = 100 − cash_buffer_pct; same denominator both sides). Invariant comment added to `gap_allocation.sql` |
| Python 3.12 host pin | ✅ Closed | `uv python pin 3.12`; host now runs CPython 3.12.13 |
| `/admin/*` endpoint auth | 🔓 **Open — carries to Phase 2** | Localhost binding mitigates today; Phase 4 mutations will need a token |
| `update_bars.py` not wired into scheduler | 🔧 Phase 2 work | Script exists; just needs scheduling |

### Phase 2 — Technical levels & weekly suggestions ⚙️ Code complete (2026-05-06); tag `v0.2.0-phase-2` deferred until first Sunday weekly suggestions email is received and read for sensibility (earliest 2026-05-10)
- All `/admin/*` routes protected by `X-Admin-Token` (Phase 1 carryover) ✓
- `update_bars()` wired into both daily and weekly jobs, plus lifespan startup ✓
- Smart bar sync (incremental from last bar date for existing tickers; 2-year backfill only for new tickers) — improvement beyond the guide's "tolerate failure" guidance ✓
- Indicators service (`/indicators` endpoint): SMA-20/50/200 via DuckDB window functions, EMA-21/RSI-14/MACD via pandas-ta, returned as `IndicatorRow` frozen dataclass ✓
- Support/resistance levels: classical pivots (weekly + monthly), MA bands as dynamic S/R, fractal swing highs/lows with unconfirmed-recent-bars excluded; persisted to `sr_level` with `UniqueConstraint(ticker, method, as_of)` ✓
- Suggestion engine (`/suggestions`, `/admin/run-weekly-suggestions`): `HALF_THE_GAP` sizing rule + 8% distance guard + $100 cash floor; `persist_suggestions()` enforces the never-overwrite-non-pending-rows audit-trail discipline ✓
- New Sunday 18:00 ET cron `run_weekly_suggestions` with 6-hour misfire grace ✓
- Daily email gains "Levels at a Glance" section + "Untracked Positions" red banner ✓
- Untracked-position detection — SQL anti-join surfaces positions held in broker but absent from `targets.yaml` as a deliberate warning in both daily and weekly emails (added when a TQQQ paper-trade was found to be silently invisible to the gap engine) ✓
- Tests grew 29 → 58 (indicators 6, levels 8, suggest 11; persist insert/update/no-overwrite/no-duplicate covered) ✓
- ADR-0006 (S/R methodology) and ADR-0007 (position sizing) shipped, both explicitly marked **⚠ Pending LLM Review** — the current "nearest level" anchor is mechanical and not a finished algorithm ✓
- Three bugs caught and fixed during build: (1) job named `weekly_orders` was renamed `weekly_suggestions` to honour the suggest-only product principle; (2) bar backfill was unconditionally re-fetching 2 years on every run, rewritten as smart sync; (3) untracked positions were silently ignored, now surfaced via warning banner ✓

**Phase 2 cleanup status:**

| Carryover / Item | Status | Notes |
|---|---|---|
| Admin-token auth (Phase 1 carryover) | ✅ Closed | All five `/admin/*` routes protected via `X-Admin-Token` dependency |
| `update_bars` wiring | ✅ Closed | Wired into lifespan startup, daily report, weekly suggestions |
| Indicators / levels / suggest services | ✅ Closed | 25 new tests; all dataclasses frozen; SQL anti-join for untracked |
| ADRs 0006 + 0007 | ✅ Written | Both flagged ⚠ Pending LLM Review for Phase 3 |
| `weekly_orders` → `weekly_suggestions` rename | ✅ Closed | Three replacement passes (snake / kebab / title-case) |
| Smart `update_bars()` (incremental backfill) | ✅ Closed | Beyond guide spec — was a real performance bug |
| Untracked-position banner | ✅ Closed | New first-class product behavior; promoted to §1 |
| First Sunday email observed | ⏳ Pending | Definition of done; earliest 2026-05-10 |
| Smoke-test row 14 (cash-buffer invariant assertion) | ❓ Verify | Pre-tag checklist verified rows 4–8 explicitly; row 14 not called out — confirm assertion exists in `test_gap.py` before tag |
| Suggestion accept/reject endpoint | 🔧 Phase 3 work | `order_suggestion.status` column supports the workflow but no `PATCH /suggestions/{id}` exists yet |

### Phase 3 — LLM-scored levels, accept/reject, news triage, and suggestion review (3–4 weeks) — current

Phase 2 shipped a mechanical suggestion engine that picks the *nearest* qualifying S/R level. That's a placeholder — "nearest" does not mean "most meaningful." Phase 3 closes that gap, lands the missing audit-trail mutation endpoint, adds news triage, and introduces a LangGraph-driven suggestion-review pipeline that reasons + critiques every weekly batch before it reaches the user.

**Phase 3 is split into three sub-phases that follow the dependency graph.** Each is a tag-worthy unit. See [`phase_3_guide.md`](phase_3_guide.md) for the overview index and [`phase_3a_guide.md`](phase_3a_guide.md), [`phase_3b_guide.md`](phase_3b_guide.md), [`phase_3c_guide.md`](phase_3c_guide.md) for the detailed step-by-step guides.

| Sub-phase | Scope | Tag |
|---|---|---|
| **3a** (~2 weeks) | `services/llm.py` wrapper + `llm_call_log`. LLM-scored confidence on S/R levels (Sonnet 4.6 single call) → confidence-weighted anchor selection. `PATCH /suggestions/{id}` + HMAC magic-link Accept/Reject buttons. ADRs 0009 (LLM guardrails), 0010 (magic-link auth); partial updates to 0006 and 0007. | `v0.3a.0` |
| **3b** (~1 week) | LangGraph introduced. News triage graph: Haiku classify → Haiku critic → conditional Sonnet arbitrate. `news_event` table. Daily 16:30 ET movers email on ≥ 5 % weekly movers. ADRs 0011 (news source priority), 0012 (LangGraph-or-not decision rule). | `v0.3b.0` |
| **3c** (~1.5 weeks) | Second LangGraph workflow: suggestion review. Sonnet per-draft rationale → Sonnet critic over the set → deterministic Python `revise` → finalize. Weekly email rationales upgrade to 2–4 sentences. ADR 0013 (suggestion review pipeline); final close-out of 0006 + 0007. | `v0.3.0-phase-3` |

**Dependency edges:** 3a → 3b (LLM client + cost guard from 3a); 3a → 3c (scored levels feed 3c); 3b → 3c (news_event feeds the critic's reasoning context).

**Workstreams within sub-phases** (expanded from the original "three workstreams A/B/C" framing):

**Workstream A — LLM level scoring + confidence-weighted anchor selection** *(in Sub-phase 3a, ~1 week — biggest correctness lift)*

- Add `confidence`, `llm_rationale`, `scored_at`, `scored_by_model`, `prompt_version` columns to `sr_level` via Alembic.
- New service `services/llm_levels.py`: pass each ticker's computed `sr_level` rows + recent OHLCV context to Claude Sonnet 4.6; return per-level confidence + brief rationale.
- Update `services/suggest.py::select_anchor` to prefer **highest-confidence anchor within distance band** rather than literal nearest. Fall back to nearest if all confidences are below threshold or the LLM call fails.
- Snapshot `confidence_at_creation` onto every new `order_suggestion` row for retrospectives.
- Hard guardrail: the LLM scores existing computed levels by exact `method` string and never invents prices or fundamental claims. Schema validation rejects bad output; engine falls back deterministically.

**Workstream B — Suggestion accept/reject endpoint** *(in Sub-phase 3a, ~2 days — blocking for real-capital trust)*

- `PATCH /suggestions/{id}` with `{ "status": "accepted" | "rejected" | "expired" }`, requires `X-Admin-Token`.
- Weekly email "Suggestions" table gains HMAC-SHA256-signed Accept/Reject buttons: `GET /suggestions/{id}/{action}?token=<expires>.<sig>`. Single-use, time-bound (TTL = `expires_at`). `MAGIC_LINK_SECRET` separate from `ADMIN_TOKEN`.
- Accept/reject mutates `order_suggestion.status` and `acted_at` only — never calls the broker.

**Workstream C — News triage via LangGraph** *(Sub-phase 3b, ~1 week)*

- LangGraph introduced. ADR-0012 anchors the "when to use a graph vs. a single call" decision rule.
- Graph: `classify (Haiku) → critic (Haiku) → conditional arbitrate (Sonnet) → persist`. Conditional edge skips Sonnet when nothing flagged.
- `services/news.py`: Alpaca News primary, Finnhub fallback. URL normalisation for cross-source dedup.
- `news_event` table with `UniqueConstraint(url_hash)`.
- New daily 16:30 ET cron `jobs/movers.py`: if any watchlist ticker moved ≥ 5 % vs. last week's close → invoke graph → persist → email. No movers → no email.
- Graph state checkpointed in SQLite via `SqliteSaver` for trace inspection. Thread-id convention `news-{ticker}-{date}`.

**Workstream D — Suggestion review via LangGraph** *(Sub-phase 3c, ~1.5 weeks)*

- Second LangGraph workflow. Graph: `gather_context → reason (Sonnet) → critic (Sonnet) → conditional revise (deterministic Python) → finalize`.
- `ReviewContext` frozen dataclass materialises gap, scored levels, recent material news (from 3b's `news_event`), indicators, account, untracked positions — *before* any LLM node runs.
- Reasoner writes a 2–4 sentence rationale per draft. Critic reviews *all* drafts as a set looking for cross-suggestion problems (combined cash-floor violations, disqualifying news, rationale-vs-math mismatches, direction-wrong against bands).
- Revise node is deterministic Python — applies critic's `suggested_changes` mechanically. **LLMs propose changes; Python applies them.** Documented in ADR-0013.
- Weekly email rationales upgrade from mechanical single lines to the Sonnet-written 2–4 sentence rationales. Mechanical `order_suggestion.reason` stays in the DB for audit.

**Deliverables:**

- (After 3a) Weekly suggestions email where each `limit_price` is chosen by LLM confidence within the distance band. Accept/Reject buttons mutate `order_suggestion.status` and `acted_at`.
- (After 3b) Daily movers email when any watchlist ticker moved ≥ 5 %, with material headlines summarised. Critic-corrected classifications visible in `news_event.arbitrated=true`.
- (After 3c) Weekly suggestions email with 2–4 sentence rationales reflecting full context. Critic visibly rejects/revises low-quality drafts before they reach the user. Phase 3 fully complete; tag `v0.3.0-phase-3`.

**Out of scope for Phase 3:** Moomoo adapter (deferred to Phase 4 — Phase 3 introduces LLM-influenced suggestion logic that needs to soak for at least 4 weeks of paper trading before real capital touches it). Web UI (Phase 5). Suggested-vs-filled reconciliation (Phase 4 — needs broker `account/activities` integration).

**Phase 3 status ⚙️ All three sub-phases code-complete (2026-05-17). Composite tag `v0.3.0-phase-3` deferred until live observation closes the pending pre-tag checklists on 3a, 3b, and 3c — earliest 2026-05-24 (two Sunday cycles for critic-rate calibration).**

**Phase 4 status ✅ Code-complete 2026-05-18.** All four workstreams shipped: reconciliation engine, MoomooAdapter, Friday weekly review email, opt-in auto-trade (mode=OFF). Tag `v0.4.0-phase-4-code-complete` pending first Friday review email. Subsequent promotion soak tags follow in calendar time per DoD table in `plans/phase_4_guide.md`. Moomoo primary-flip is a separate manual decision after ≥4 weeks of parallel-run soak.

| Sub-phase | Code-complete | Tag | Tag-gated observations |
|---|---|---|---|
| 3a | 2026-05-12 | `v0.3a.0` (pending) | One Sunday email with `(conf X.XX)` and Sonnet rationale in `reason`; Accept click → `acted_at` populated |
| 3b | 2026-05-15 | `v0.3b.0` (pending) | One movers email with `arbitrated=true` row in `news_event`; `mover_state` advances correctly on second crossing |
| 3c | 2026-05-17 | `v0.3.0-phase-3` (pending) | Sunday email shows Sonnet 2–4 sentence rationales; "Not Suggested This Week" section appears; `anchor_method` populated; critic reject-or-revise rate in 10–25% across 2 Sundays |

**Carryovers into Phase 4 from the Phase 3c completion review:**

- **MU silent-failure in `score_all_tickers_parallel()`** — same class of bug as Phase 3c Bug 2 but in a different code path; the `SkippedRow` discipline applied to `generate_suggestions()` was not extended here. Phase 4 §1 fixes via specific exception handling, `exc_info=True` logging, and user-visible surfacing.
- **Distance-guard calibration revisit** — Phase 3c bumped `max_distance_pct` from 8% to 15% to unblock MU. This is fix-for-MU rather than principled calibration; Phase 4 reconciliation data (fill rates by anchor distance) is the right input for revisiting whether 15% should stick or be per-ticker.
- **Six pending pre-tag observations across 3a/3b/3c** — Phase 4 §0 pre-flight blocks on these being green.

**Phase 3 stats:** 189 unit tests + 1 integration (up from 28 at Phase 1 close). Five new ADRs (0011, 0012, 0013, 0016) plus two closures (0006, 0007). Three LangGraph workflows (news triage in 3b, suggestion review in 3c, and the placeholder-acknowledged-but-unbuilt level-scoring graph from 3c §6.5a). Phase 3 retrospective identifies `DetachedInstanceError` as the project's most-recurrent bug (Phase 1 `BrokerAccount` → Phase 3b `MoverState` → Phase 3c `gather_context`); the `gather_context_node` pattern is now a first-class architectural primitive captured in CLAUDE.md convention #9 + gotcha #12.

### Phase 4 — Weekly review workflow + Moomoo adapter (1.5–2 weeks)
- Friday EOD job: build the weekly review:
  - Realized PnL for the week.
  - **Orders suggested vs. filled reconciliation** — pull `account/activities` from the broker, match against `order_suggestion` rows by ticker + side + week, write to a new `order_execution` table. Did you actually place the suggested order? At what price? When?
  - New gap vs. target after this week's moves.
  - Big events flagged by the daily monitor.
  - **Proposed orders for next week** (Phase 2 engine, refreshed with Phase 3 LLM-scored levels).
- (Accept/reject endpoint already ships in Phase 3; Phase 4 only adds the weekly digest of which suggestions were accepted, rejected, expired, or filled.)
- **Moomoo adapter** (`brokers/moomoo.py`): implements `BrokerAdapter` against OpenD on `host.docker.internal:11111`. Switchover via `BROKER` env var. Run for 2–4 weeks in parallel against Alpaca (read-only) before flipping primary. See ADR-0001.
- Deliverable: Friday 5 PM ET weekly review email containing the suggested-vs-filled reconciliation, plus an honest audit trail of "what I suggested vs. what I accepted vs. what actually filled." Optional second deliverable: Moomoo running side-by-side with Alpaca, both reporting consistent positions before the switchover.

### Phase 4.5 — Target adjustment & rebalance reviews (3–5 days)
The product needs to help you **evolve** the target allocation over time, not treat it as frozen.

Triggers the system should detect / schedule automatically:

| Trigger | How it's detected | What the system does |
|---|---|---|
| **Funds added / withdrawn** | Daily comparison of `broker_account.cash_usd + equity_usd` vs. previous snapshot, minus realized PnL. A delta > configurable threshold (e.g., $500) that isn't explained by market moves flags a funds event. Also cross-check Alpaca's `GET /v2/account/activities` (TRANS/JNLC). | Sends a "Funds detected (+$X). Review targets?" email with a link to edit. |
| **Quarterly review** | Cron: first trading day of Jan / Apr / Jul / Oct. | Sends a "Quarterly review due" email with current drift, performance vs. benchmark, and a prompt to edit targets. |
| **Annual review** | Cron: first trading day of the year (plus optional tax-year-end reminder in late December for US). | Deeper report + target review prompt. |
| **Manual** | You hit the `/targets` endpoint or edit the YAML. | Version the change, log `target_change_event`, recompute gap on next job. |

Implementation:
- Targets are edited via **either** (a) committing to `targets.yaml` in a git repo the app watches, or (b) a simple FastAPI `/targets` form (later `/targets` page in Phase 5).
- On any edit: **validate** (weights sum to 100 ± 0.5%, no target outside [0, 50]% unless explicitly overridden), **diff against previous**, **close the previous `target_allocation` row with `effective_to = now`**, insert new rows with `effective_from = now`, write `target_change_event`.
- The weekly suggestion engine always reads the **currently-effective** targets. Older reports can recompute historical gap against the targets that were live at that time.
- Guardrail: if a single edit would shift any ticker's weight by > 10 %, require a confirmation step (email magic-link click) before applying. Prevents fat-finger changes.

Deliverable: you can add funds (even $1,000) and within 24 h get an email saying "you added $1,000, here's your current drift, want to edit targets or just deploy to existing weights?" with a one-click "deploy to existing weights" that just feeds the gap engine.

### Phase 5 — Productization (2–3 weeks)
- Add React dashboard (Vite + Tailwind) reading the same FastAPI.
- Targets edit page with diff preview and guardrails from Phase 4.5.
- Auth (Clerk or Supabase).
- Multi-tenant data model: every row gets `user_id`.
- Split storage: **Postgres** for OLTP/auth/user rows, **DuckDB** (or MotherDuck in cloud) for per-user analytics/bars.
- Encrypted per-user broker credentials (envelope-encrypted with a KMS key or `cryptography.fernet` with a rotating master key).
- Pluggable per-user broker adapter — user picks Alpaca, Moomoo, or IBKR at connect time.
- **Mandatory pre-launch removal:** the `LLM_CLI_PATH`-with-consumer-OAuth path that Phase 3b shipped (and that the solo project owner uses for personal use) **must be removed entirely** before any second user signs up. The solo personal-use exception in ADR-0016 explicitly does not extend to multi-tenant deployment — what's gray-area-tolerated for one user becomes unambiguous OAuth abuse for many. All users on `LLM_BACKEND=anthropic_api` in Phase 5; remove the `agent_sdk` option from the user-facing config entirely or restrict it to deployments authenticated via per-user `ANTHROPIC_API_KEY` only.
- Deliverable: a second user can sign up, connect their Alpaca or Moomoo, and get their own weekly emails.

### Phase 4.6 — Opt-in auto-trade execution (~1 week build + 4–6 week soak per promotion)

**This phase overturns the suggest-only default carefully — by adding auto-trade as an opt-in mode behind multiple guards, not by changing the default.** Suggest-only stays as `auto_trade_mode=OFF`, which is the value every new install gets. Promotion to `DRY_RUN` or `LIVE` requires deliberate admin action and a soak window.

**Three-state mode controlled by an app-wide switch with admin access:**

| Mode | Behaviour |
|---|---|
| `OFF` (default) | Existing suggest-only behaviour preserved. No broker order calls. |
| `DRY_RUN` | Auto-trade module computes what *would* be placed for every `accepted` suggestion. Writes `order_execution` rows with `dry_run=true`. **Never calls the broker.** Email confirms what would have happened. |
| `LIVE` | Calls the broker via `BrokerAdapter.submit_order` (the first time anything outside `brokers/` is allowed to trigger this method). Writes `order_execution` rows with `dry_run=false`. |

**Required guards before any broker call (all enforced atomically):**

- **Trigger discipline:** fires only on `order_suggestion.status='accepted'`. Never on `pending`. Never on fresh suggestions the user hasn't seen.
- **Hard caps:** per-order $, per-day $, per-week $-per-ticker, per-day order-count. Hitting any cap flips mode to `OFF` and emails a notification.
- **Wash-sale guard upgraded from stub to blocking:** if a sell at a loss for the same ticker happened in the last 30 days, the buy is dropped with a logged reason. (Phase 4 reconciliation provides the `order_execution` history this check needs.)
- **Idempotency:** every order's client ID is derived from the suggestion ID (`client_order_id = f"sug-{suggestion.id}"`). Same suggestion cannot be placed twice even on a double-fire. The guard is cleared (status → `broker_cancelled`) when an order is cancelled via `POST /admin/cancel-all-orders`, allowing auto-trade to re-place it.
- **GTC limit orders:** orders are placed as `time_in_force="gtc"` — they stay open at the broker until filled or explicitly cancelled. The daily expiry sweep (16:20 ET) cancels any open GTC order whose suggestion has passed `expires_at`, then marks the suggestion expired.
- **Read-back reconciliation within 60 seconds:** every placed order is fetched back from the broker. Mismatch flips mode to `OFF`, alerts.
- **Kill switch:** `POST /admin/auto-trade/emergency-stop` flips mode to `OFF` and cancels all auto-trade-placed open orders from the last 24 hours.
- **Manual cancel:** `POST /admin/cancel-all-orders` cancels open orders without touching mode — useful for repricing when limit prices are stale mid-week. Suggestions stay `accepted` and are re-placed on the next auto-trade run.

**Broker scope progression (each step gated on a soak window):**

1. Alpaca paper account in `DRY_RUN` for 2 weeks (validates the framework, no real money).
2. Alpaca paper account in `LIVE` for 4 weeks (validates the broker call path, still no real money).
3. Alpaca live account in `LIVE` for 4 weeks (real money, small capital).
4. Moomoo in `LIVE` (depends on Phase 4 Moomoo adapter shipping first).

Each promotion is a deliberate admin command (not a config edit), logged to `auto_trade_promotion_log` for audit. Demoting back to `OFF` is always one click.

**Deliverable:** A single accepted suggestion in the weekly email becomes a real (or dry-run) order placed at the limit price on the chosen broker, with idempotent client order IDs, read-back confirmation, and a populated `order_execution` row. Tag `v0.4.6.0` after first successful Alpaca paper `DRY_RUN` week; subsequent tags (`v0.4.6.1`, `v0.4.6.2`, …) mark each promotion step.

**ADRs:** ADR-0014 (auto-trade mode discipline + promotion gates), ADR-0015 (kill switch design + recovery semantics).

See `phase_4_6_guide.md` for the step-by-step build.

### Phase 4.7 — Context-Aware Weekly Order Sizing (2026-05-26)

**Motivation:** Phase 4.5 built Friday market-context synthesis via Tavily + Sonnet. This phase makes that context *drive* Sunday suggestion sizing: a new `context_adjust_node` is spliced into the suggestion-review graph between `reason` and `critic`. It applies (a) a deterministic earnings gate using a fresh Finnhub calendar fetch and (b) a bounded Sonnet size multiplier from Friday's persisted market narrative.

**Key design decisions (see ADR-0021):**
- Bounded context influence: Tavily output may now reach `context_adjust_node` for quantity scaling only, within Python-clamped `[context_size_min, context_size_max]` (default 0.25–1.5). The LLM never originates tickers, sets prices, or makes trade recommendations.
- Earnings gate uses Finnhub structured calendar, not Tavily free-text `forward_events`.
- Critic bumped to v2 (rule 6: respect prior defensive sizing adjustments).
- Friday persists context with `week_of=_next_monday()` key; Sunday loader uses the same key.

**New components:**
- `services/earnings.py` — `EarningsClient` Protocol + `FinnhubEarningsClient` + `FakeEarningsClient` + factory
- `services/weekly_context.py` — `persist_weekly_context()`, `load_latest_weekly_context()` helpers
- `graphs/suggestion_review.py` — `context_adjust_node`, `_deeper_anchor`, `_find_level` helpers; `ReviewContext` extended
- `prompts/context_size_v1.txt`, `prompts/suggestion_critic_v2.txt`
- `models.py` — `WeeklyMarketContextRow` table; `OrderSuggestion` audit cols: `base_qty`, `size_factor`, `context_note`
- Email templates: show `(base N · ×X.XX)` and `context_note` for adjusted suggestions

**Deliverable:** Sunday email shows sensible size adjustments with `context_note` on ≥1 suggestion for two consecutive weeks. Tag `v0.4.7.0`.

**ADRs:** ADR-0021 (context-aware sizing design decisions).

### Phase 4.9a — Multi-broker plumbing + per-broker reports (2026-05-30)

**Motivation:** Let one user hold positions across multiple broker accounts at once (Alpaca + Moomoo first) and receive **separate** daily/weekly emails per broker. Suggest-only holds across all brokers; auto-trade LIVE stays Alpaca-only (each new broker repeats its own Phase 4.6 soak ladder).

**Key design decisions (see ADR-0024):**
- `broker_account_id` (= `broker_account.account_ref`, a stable partition key) on every per-account table; `broker_account` is dual-purpose (identity + state). No new identity table, no UUIDs in 4.9a; plain column, no DB FK.
- Per-broker `auto_trade_state` replaces `meta.auto_trade_mode`; each broker has its own OFF→DRY_RUN→LIVE soak ladder and its own guards/caps/kill switch.
- News, S/R levels, and weekly market context stay **user-level** (one synthesis serves all brokers).
- Cross-broker wash-sale is deliberately per-broker in 4.9a (tax-lot/cross-account is Phase 6).

**New components:** `services/accounts.py`; `brokers.make_account_adapter` / `build_account_adapters`; `*_all_brokers` job loops; per-account targets files `data/targets/<id>.yaml`; `POST/GET/DELETE /admin/broker-accounts`; account-scoped endpoints (`?broker_account_id`); migrations `7d25844a8a9a` (adopt create_all tables into Alembic), `d8589fe198cf` (partition key + auto_trade_state), `6a4a9fada1dc` (NOT NULL).

**Deliverable:** connect a second broker (Moomoo paper) on top of Alpaca and receive two separate daily + two weekly emails, with audit columns populated per-broker and existing single-broker history carried through migration. Tag `v0.4.9a.0`.

**Out of scope (Phase 4.9b):** household target allocation, consolidated summary email, funds-added detection, quarterly/annual review crons, magic-link target-edit guard. **Out of scope (own sub-phases):** IBKR + Tiger adapters (ADR-0025/0026). **Phase 6:** tax-lot / cross-broker wash-sale.

**ADRs:** ADR-0024 (multi-broker single-user data model).

### Phase 6 — Paper → live hardening (1 week work, 4–6 weeks soak)
- Add kill switches: daily max suggested spend, max position size, max drift before halting new buys.
- Backtest the suggestion engine against the last 2 years — does "buy at support" actually fill often enough?
- Keep paper-trading for at least 4 weeks after Phase 4 ships. Only switch to live Alpaca keys once you've manually executed at least 8 weekly suggestion batches and are happy with the quality.

---

## 7. Key design decisions still open (flag these now)

1. **What "support/resistance" method do you actually trust?** Pivot points are mechanical and boring but work for long-term entries. Swing-based is subjective. My bet: start with pivots + MAs, add swing-based later.
2. **Rebalancing bands.** 5 % absolute vs. 25 % relative is a meaningfully different strategy. Pick one before coding the gap engine.
3. **Fractional shares?** Alpaca supports them. If yes, suggestions get more precise but executions are slightly different.
4. **Position sizing per suggestion.** "Close half the gap" is one rule; "fixed dollar amount per week" is another. Different emotional properties.
5. **News sources over the long run.** Alpaca News + Finnhub is fine now, but if you ever add an earnings calendar or insider trades it'll be a separate integration (Finnhub covers both).
6. **How to handle dividends, splits, corporate actions** in the snapshot table — start with Alpaca's reported avg_cost; it handles most of this for you.
7. **Backtesting infrastructure.** Not in v1, but plan the data model so bars + suggestions can be replayed. DuckDB + Parquet makes this almost free to add later.
8. **Target drift tolerance before prompting a review.** Separate from rebalancing bands — this is "when to *ask* if you want to rebalance" (e.g., any ticker > 1.5× its band for 4 consecutive weeks) vs. when to *act*. Start conservative.
9. **AUD ⇄ USD FX cadence.** If using Alpaca, funds convert via Rapyd at transfer time — no FX automation needed. Moomoo AU handles it in-app. Just record the FX rate at time of deposit in `funds_event` for accurate performance attribution.
10. **Alembic vs inline migrations** *(carried over from Phase 0)*. Phase 0 shipped without Alembic, using inline `ALTER TABLE IF NOT EXISTS` in `init_db()`. This works for adding columns but breaks down for renames, type changes, and table moves. Phase 1 should write ADR-0002 and ideally adopt Alembic before Phase 2 brings substantial schema churn.

---

## 8. Risks & compliance

- **Regulatory:** Suggest-only keeps you firmly out of RIA territory. If you ever go multi-user AND automated, you are immediately in regulated-advice / broker-relationship land. Don't cross that line casually.
- **Wash sales** (US): if you sell at a loss and rebuy within 30 days, disallowed. Your weekly engine can unintentionally trigger this. Add a check: don't suggest buying a ticker you sold at a loss in the past 30 days.
- **Data quality:** yfinance has known glitches. Always reconcile positions against the broker's own report nightly.
- **LLM hallucination:** never let the LLM output price targets or claims about fundamentals. Restrict it to "summarize this headline" and "is this about earnings/guidance/M&A/legal/none."
- **Secrets:** Alpaca keys in `.env` with `chmod 600` for v1. Never commit. Never email. Consider rotating every 90 days.
- **Backup:** DuckDB file + Parquet folder → nightly `rclone` to Backblaze B2 or S3. Cheap insurance.
- **AU tax:** long-term US equities held by an AU resident have CGT implications (50 % CGT discount after 12 months) plus W-8BEN for US withholding. The product should record hold-period start dates so you can flag "this lot is 11 months old — holding another month unlocks the discount" before the engine suggests a trim.

---

## 9. Cost estimate (solo, always-on)

| Item | Monthly |
|---|---|
| Mini-PC (one-time) or VPS (Hetzner CX22) | ~$5 |
| Alpaca (commission-free, free IEX data) | $0 |
| Finnhub (free tier) | $0 |
| Claude API (Haiku triage + occasional Sonnet) | $2–10 |
| Domain + dynamic DNS | ~$1 |
| **Total** | **~$10–20/mo** |

At Phase 5 add Postgres (still free on most providers at your scale), auth provider ($0–25/mo), and email service ($0–15/mo).

---

## 10. What to build first (this weekend)

A 1-day spike that validates the whole stack before you invest weeks:
1. Create Alpaca paper account (any AU resident can — paper has no country restrictions).
2. `uv add alpaca-py pandas-ta anthropic duckdb duckdb-engine pyyaml jinja2`
3. Write a 100-line Python script that:
   - pulls positions from Alpaca paper,
   - loads a `targets.yaml`,
   - writes one daily snapshot into a DuckDB file,
   - computes the gap in SQL (`SELECT ... FROM positions_snapshot JOIN target_allocation ...`),
   - fetches 1 ticker's bars and computes its 20/50/200-SMA and classical pivots using DuckDB window functions,
   - prints a "suggestion" to stdout,
   - sends it to your email via Gmail SMTP.
4. If that works end-to-end in a weekend, you know every external dependency is fine, and Phase 0's scaffolding becomes a mechanical job.

---

## 11. Broker migration path — Alpaca → Moomoo

Your intent is: **build and test on Alpaca (paper first, then small live), later route real long-term capital via Moomoo.** Making that painless comes down to three design rules followed from day 1:

**Rule 1 — The product only ever talks to `BrokerAdapter`, never to a vendor SDK directly.**
```
class BrokerAdapter(Protocol):
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def get_activities(self, since: datetime) -> list[Activity]: ...  # deposits, dividends, fees
    def get_bars(self, ticker: str, start, end, timeframe) -> DataFrame: ...
    def get_corporate_actions(self, ticker: str) -> list[CorpAction]: ...
```
Alpaca's client and Moomoo's `OpenQuoteContext`/`OpenSecTradeContext` both get wrapped by adapters that produce the same dataclasses. The rest of the app is broker-agnostic.

**Rule 2 — Market data is separable from execution data.**
Even when you eventually trade on Moomoo, you can (and probably should) keep using Alpaca's free market-data feed for bars and news. This avoids OpenD's quota limits. The only things that must come from the "active" broker are positions, account balance, activities, and corporate actions. This is reflected in the adapter split above — treat "bars" as an interchangeable data source.

**Rule 3 — Store broker-specific identifiers separately from domain IDs.**
A `position` in the DB is keyed by `(user_id, ticker)`, not by Alpaca's `asset_id` or Moomoo's internal code. Keep the vendor code in a sidecar column so reconciliation still works, but your logic never depends on it.

**The actual switchover, once you're ready:**
1. Install Moomoo OpenD on the host Mac (not in Docker). Log in with your Moomoo AU account. Start it on port 11111.
2. Write `MoomooAdapter` (~300–500 lines). Most fields map cleanly; the tricky bits are (a) Moomoo returns positions in USD *and* AUD-equivalent — store USD to match the rest of the system, and (b) corporate actions come from a separate `OpenQuoteContext` call.
3. Add a one-off reconciliation job: on switch-day, run both adapters, diff the position snapshots, and only flip the `BROKER` env var once they agree to the share.
4. Backfill `funds_event` from Moomoo's deposit history so performance attribution remains continuous across the move.
5. Suggestions engine is unchanged. Email templates are unchanged.

Budget: ~1 week to write + test `MoomooAdapter` on paper/sandbox, then a 2-week parallel run (reading from both, trading on one) before fully cutting over.

---

*This plan is a starting point — iterate on it in the open questions section as you build.*
