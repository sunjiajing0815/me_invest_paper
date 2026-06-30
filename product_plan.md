# Long-Term Investor Assistant — Product Plan (v1)

**Owner:** Jane · **Date:** 2026-04-24 (last update 2026-05-20) · **Stage:** Building — Phases 0–4.5 code-complete; pre-tag observation window across Phases 3, 4, and 4.5 in progress (manual checklist in `pre_phase_5_manual_testing_checklist.md`); Phase 5 starts once Block A–E checklist items clear and tags `v0.3.0-phase-3`, `v0.4.0-phase-4-code-complete`, `v0.4.5.0` are pushed. Auto-trade promotion tags (`v0.4.1` → `v0.4.4`) run on a separate 14–16-week calendar timeline in parallel with Phase 5 development.

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

### Phase 4 — Reconciliation, Moomoo adapter, weekly review, and opt-in auto-trade execution (~3–3.5 weeks code + 14–16 weeks staged soak)

Phase 4 is the largest single phase of the project. It bundles four tightly-coupled workstreams that all touch `order_execution` — separating them artificially (as in earlier drafts that split Phase 4 from Phase 4.6) creates more friction than it removes. They share one schema, one data flow, and one soak progression.

**The four workstreams:**

1. **Reconciliation engine** — daily 16:45 ET cron pulls `account/activities` from the broker, matches against `order_suggestion` rows via four priority rules (exact `client_order_id` for auto-trade activities, heuristic match within 48h time + 0.5% price tolerance, ambiguous flagged for `manual_review`, untracked manual trades logged). Writes the `order_execution` table — the audit-trail data every other workstream depends on.

2. **Moomoo adapter** (`brokers/moomoo.py`) — implements `BrokerAdapter` against OpenD on `host.docker.internal:11111`. Ships read-only first: a parallel-run cron polls Moomoo positions/account/activities and compares against Alpaca for 4+ weeks before any `BROKER=moomoo` flip. Switchover is a deliberate later step, not part of any Phase 4 tag.

3. **Weekly review email** — Friday 17:00 ET cron. Six sections: realized PnL, suggested-vs-filled reconciliation, drift state after the week, material movers from the daily monitor, preview of next Sunday's suggestions, and (during soak) Moomoo parallel-status. Pure digest, not a generator.

4. **Opt-in auto-trade execution** — three-state mode (`OFF` / `DRY_RUN` / `LIVE`), default `OFF`. Fires only on `order_suggestion.status='accepted'` rows. Hard caps (per-order, per-day, per-week-per-ticker, per-day count) flip mode to `OFF` automatically on breach. Wash-sale guard reads `order_execution` history; idempotent via `client_order_id = f"sug-{suggestion.id}"`; read-back reconciliation within 60s of every place; kill switch endpoint cancels open auto-trade orders in last 24h.

**How auto-trade and reconciliation interact** (the key integration design):

`order_execution` has two writers, one shared schema, one unique constraint `(broker_order_id, broker)` as the dedup boundary.

- **Auto-trade writes first**, at 9:35 ET. After `adapter.submit_order()` + 60-second read-back, it `INSERT`s an `order_execution` row with `broker_order_id`, `client_order_id=f"sug-{N}"`, `status='accepted_for_routing'`, `filled_qty=0`. DRY_RUN writes the same shape with `dry_run=true`, `broker_order_id=NULL`.
- **Reconciliation updates that row later**, at 16:45 ET. Same `broker_order_id` matched via unique constraint → `UPDATE` fill fields (`filled_qty`, `filled_price`, `filled_at`, `status` transitioning to `filled` / `partially_filled` / `expired`), computes `realized_pnl_usd` via FIFO for sells.

Matching rule order from Phase 4's reconciliation is upsert-shaped:
- Rule 1 (exact `client_order_id` match): find the row auto-trade already inserted; update fill fields.
- Rule 2 (heuristic match within window): no existing row; insert one (manual trade or order placed without auto-trade tracking).
- Rules 3 (ambiguous) and 4 (untracked) unchanged.

**DRY_RUN is invisible to reconciliation** — filtered out at every matcher query and every wash-sale-guard query (`WHERE dry_run = false`). Simulated losses never block real buys.

**Wash-sale guard's data dependency:** auto-trade at 9:35 ET reads reconciled history through close-of-yesterday. Reconciliation at 16:45 ET computes `realized_pnl_usd` for any sells from that day. Tomorrow's 9:35 guard sees yesterday's losses. The guard is meaningful from day one of DRY_RUN soak because reconciliation has been running independently for at least 1–2 weeks before auto-trade promotes to DRY_RUN.

**Auto-trade mode states:**

| Mode | Behaviour |
|---|---|
| `OFF` (default — fresh install, after restart, after any guard failure) | Suggest-only behaviour preserved. No broker order calls. |
| `DRY_RUN` | Computes what *would* be placed for every `accepted` suggestion. Writes `order_execution` rows with `dry_run=true`. **Never calls the broker.** Daily summary email confirms what would have happened. |
| `LIVE` | Calls `BrokerAdapter.submit_order` — the first and only code path outside `brokers/` permitted to do so. Writes `order_execution` rows with `dry_run=false`. Followed by read-back-within-60s; mismatch flips mode to `OFF` and cancels via kill switch. |

**Tag-and-soak scheme** — one phase, multiple tags as soak windows close cleanly:

| Tag | Milestone | Calendar gate |
|---|---|---|
| `v0.4.0-phase-4-code-complete` | All four workstreams shipped; auto-trade `OFF` in production | Code-complete (3–3.5 weeks from start) |
| `v0.4.1-paper-dry-run` | Auto-trade promoted to `DRY_RUN` on Alpaca paper, clean for 2 weeks | After ~2 weeks of reconciliation accumulating execution history + 2 weeks DRY_RUN |
| `v0.4.2-paper-live` | Auto-trade `LIVE` on Alpaca paper for 4 weeks clean | After `v0.4.1` + 4 weeks |
| `v0.4.3-alpaca-live` | Auto-trade `LIVE` on real Alpaca for 4 weeks clean (real money, small capital) | After `v0.4.2` + 4 weeks |
| `v0.4.4-moomoo-live` | Auto-trade `LIVE` on Moomoo for 4 weeks clean | After `v0.4.3` + Moomoo parallel-run soak complete + `BROKER=moomoo` flip + 4 weeks |

End-to-end calendar time from `v0.4.0` to `v0.4.4` is roughly 14–16 weeks. Code-complete to first auto-trade tag (`v0.4.1`) is ~3–4 weeks total. Each promotion is a deliberate admin command via `POST /admin/auto-trade/promote` (separate `AUTO_TRADE_PROMOTION_TOKEN` from the `ADMIN_TOKEN`), logged to `auto_trade_promotion_log`. Demoting to `OFF` is always one click and instant.

**The Moomoo primary flip** (`BROKER=moomoo`) is independent of the auto-trade soak — it gates `v0.4.4` (auto-trade can't run LIVE on Moomoo without Moomoo being primary) but is itself a separate manual decision once the 4-week Moomoo parallel-run soak is clean. You can run on Moomoo with auto-trade `OFF` (suggest-only on Moomoo) for as long as you like before promoting auto-trade there.

**Deliverable:** A single accepted suggestion in the Sunday email → an order placed at the limit price on the chosen broker, idempotent client order ID, read-back-confirmed, populated `order_execution` row, daily summary email confirming what was placed (or dry-run-simulated), and the Friday review showing the week's fills reconciled back to the suggestions that drove them.

**ADRs:** ADR-0014 (auto-trade mode discipline + promotion gates), ADR-0015 (kill switch design + recovery semantics), ADR-0017 (reconciliation matching), ADR-0018 (Moomoo parallel-run protocol), ADR-0019 (weekly review composition); update to ADR-0007 (distance-guard calibration data from §6 of the guide).

See `phase_4_guide.md` for the step-by-step build.

### Phase 4.5 — Tavily-driven weekly market context ⚙️ Code complete (2026-05-20); tag `v0.4.5.0` deferred until 2 consecutive Friday weekly-review emails arrive with non-empty Weekly Market Context content (earliest 2026-05-29)

*(Note: the slot originally labeled Phase 4.5 in earlier drafts held "target adjustment & rebalance reviews." That content was moved into Phase 5 in the prior restructure; the slot is reused here for a different, smaller piece of work. Old "Phase 4.5" references in pre-2026-05 docs should be read as today's Phase 5 target-adjustment workstream.)*

**What shipped:**

- `services/tavily.py` — `TavilyClient` Protocol + `TavilyConcreteClient` (lazy SDK import, monthly cap, exception handling) + `FakeTavilyClient` test double + `make_tavily_client()` factory ✓
- `services/weekly_context.py` — `build_weekly_market_context()` with ~12–16 Tavily fanout queries (2 macro, 1-per-sector, 1-per-ticker, 2 forward-looking) + Sonnet synthesis + `WeeklyMarketContext` frozen dataclass ✓
- `prompts/weekly_context_v1.txt` — hard rules (no price targets, no buy/sell, no claims beyond Tavily input, no invented entities) ✓
- Friday weekly review email gained Section 7 "Weekly Market Context" between auto-trade and Moomoo sections; guarded by `{% if review.market_context %}` so the section is fully absent (not broken) on Tavily outage ✓
- Five-step graceful degradation chain (no key → Fake → `None` → section absent ; cap reached → empty + warning ; SDK exception → empty + traceback ; all-empty → `None` ; LLM schema failure → citations-only) ✓
- 16 new tests (11 Tavily + 5 weekly context); total 260 (up from 240 at Phase 4 close) ✓
- ADR-0020 written and accepted (Protocol over ABC; SDK pinned `>=0.6,<0.7` for Nebius acquisition durability; swap path to Serper/Brave/Perplexity documented; informational-only constraint architecturally enforced) ✓

**Bugs caught and fixed during build:**

| Bug | Type | Resolution |
|---|---|---|
| `_domain("https://wsj.com/...")` returned `"sj.com"` — `str.lstrip("www.")` strips the *character set* `{'w', '.'}` not the literal prefix | Implementation-time | Replaced with `removeprefix("www.")`; caught by test before merge |
| Prompt file lookup tried `weekly_context_vv1.txt` — `f"...v{prompt_version}.txt"` with `prompt_version="v1"` produced double-v | Implementation-time | Changed format string to `f"weekly_context_{prompt_version}.txt"`; caught by test |
| Weekly review email showed "Moomoo OpenD running in PARALLEL" on every install | Post-deploy | `opend_host` default changed from `"host.docker.internal"` to `""`; `OPEND_HOST` must now be explicitly set to trigger parallel-running status |
| `POST /admin/auto-trade/promote` returned 500: `can't subtract offset-naive and offset-aware datetimes` | Post-deploy | Added `.replace(tzinfo=UTC)` to `last_entry.ts` before the comparison |

**Improvement during session:** `(alpaca_paper, LIVE)` soak window reduced from 14 days → 0. The meaningful gate is `alpaca_live` (28 days paper LIVE). Paper trading has no real money at stake, so the OFF → DRY_RUN → LIVE on paper progression is now immediate. The `alpaca_live` and `moomoo` soak windows remain at 28 days and must not be similarly relaxed — that discipline is what prevents auto-trade disasters.

**Additional fix folded in:** movers Monday lookback widened from 24h → 48h. Weekend and Friday news otherwise falls outside the window by Monday market open. URL-hash dedup prevents double-insertion.

**Phase 4.5 cleanup status:**

| Item | Status | Notes |
|---|---|---|
| Code complete | ✅ Done | 2026-05-20 |
| 16 new tests (11 Tavily + 5 weekly context) green | ✅ Done | 260/260 unit tests pass |
| `ruff` + `mypy` clean | ✅ Done | No new errors on Phase 4.5 files |
| ADR-0020 written | ✅ Done | Accepted |
| CLAUDE.md + README + `.env.example` updated | ✅ Done | New env vars, repo layout, gotchas 21–23 |
| Bug 1 (`_domain` lstrip) | ✅ Closed | Caught by test before merge |
| Bug 2 (prompt filename double-v) | ✅ Closed | Caught by test before merge |
| Bug 3 (opend_host default) | ✅ Closed | Default flipped to `""` |
| Bug 4 (promote endpoint datetime) | ✅ Closed | `.replace(tzinfo=UTC)` applied |
| `TAVILY_API_KEY` configured in production `.env` | ⏳ Pending operational step | Manual checklist item A1 |
| First Friday weekly review email with non-empty Weekly Market Context section | ⏳ Pending | Earliest 2026-05-22 |
| Second Friday email — consistency check | ⏳ Pending | Earliest 2026-05-29 → enables tag |
| Negative-test: `TAVILY_API_KEY=""` → section absent (no broken HTML) | ⏳ Pending | Manual checklist item C1 |
| Critical reading of synthesis quality (factual, no recommendations leaking past guardrails) | ⏳ Pending | Manual checklist item B5 |

**Manual testing checklist:** `pre_phase_5_manual_testing_checklist.md` consolidates Phase 4.5 pre-tag observations alongside accumulated pre-tag debt across Phases 3 and 4. Work through Blocks A–C today, then Blocks D–E over the next two weeks, then tag.

**Why kept separate from Phase 4 rather than folded in:** Phase 4 is already large (4 workstreams + 14–16 weeks of staged soak). Tavily is conceptually distinct work (web search vs broker integration), ships in ~3–5 days, and depends only on Phase 4 *code-complete* (`v0.4.0-phase-4-code-complete`) — not on any auto-trade soak completion. Runs on its own calendar timeline alongside the auto-trade promotion soaks.

**Tavily acquisition durability note:** Tavily was acquired by Nebius in February 2026. API surface remains stable per the official `tavily-python` SDK as of code-complete, but the acquisition is a real durability signal. The integration is deliberately thin (a single `services/tavily.py` wrapper behind a Protocol) so a future swap to a competitor (Serper, Brave Search, Perplexity, Exa) costs less than a day. ADR-0020 documents the re-evaluation cadence: every 6 months or any time Tavily pricing changes ≥ 20%.

**Why Tavily is deliberately NOT used in the Phase 3b daily movers pipeline** (architectural decision worth preserving so future agents don't "standardize"): three reasons in priority order — (1) Tavily's general-web index refreshes on a crawl cadence measured in hours-to-day; Alpaca News (Benzinga-backed) indexes financial headlines within minutes, disqualifying Tavily for same-day mover explanations; (2) Alpaca/Finnhub are purpose-built for ticker-tagged financial news with much higher signal-to-noise than general web search; (3) the two use cases are genuinely different — daily movers ask "what specific same-day company news moved this ticker?"; weekly context asks "what macro/sector themes should I be aware of?" — same-tool-everywhere instinct dilutes both. Tavily-as-third-fallback in Phase 3b (for the case where Alpaca + Finnhub both come up empty on a ≥5% mover) is a sensible Phase 5+ enhancement if "no news explanation" annotations turn out to be common, but is **not** in scope here. See `phase_4_5_guide.md` §8.

**Tavily-augmented weekly context vs the existing daily news pipeline:**

| Existing news pipeline (Phase 3b) | What Tavily adds (Phase 4.5) |
|---|---|
| Per-ticker headlines (Alpaca News + Finnhub, Benzinga-skewed) | Broader web (FT, WSJ, Bloomberg, sector publications) |
| Daily, triggered by ≥5% weekly-mover threshold | Weekly, unconditional |
| Per-ticker classification (material / sentiment / summary) | Macro + sector narrative synthesis |
| No Fed / sector rotation / macro coverage | Macro and sector queries as first-class |
| No forward-looking | Earnings calendar + Fed events + regulatory deadlines next week |

**Cost achieved:** ~12–16 searches per Friday run × 4 runs/month ≈ 60/month, well under Tavily's 1,000-search free-tier cap. Sonnet synthesis call ~$0.02–0.05/week. Total under $1/month at single-user scale (matches the planning estimate).

**Hard guardrails enforced:** Tavily results are *evidence*, never recommendations. The synthesis prompt forbids price targets, buy/sell recommendations, and fundamental claims beyond what's visible in the Tavily-returned content. JSON-schema validation on Sonnet output via the Phase 3a `LLMClient.call()` infrastructure. `WeeklyMarketContext` is a frozen dataclass with no code path to `generate_suggestions()`, `run_auto_trade_pass()`, or any broker adapter — informational-only, enforced architecturally rather than by convention.

**ADRs:** ADR-0020 (Tavily as the weekly-context web-search provider; rationale, alternatives considered, swap path) ✓

See `phase_4_5_guide.md` for the step-by-step build and `pre_phase_5_manual_testing_checklist.md` for the manual observation gates.

### Phase 4.7 — Context-aware weekly order sizing ⚙️ Code complete (2026-05-26); tag `v0.4.7.0` deferred until 2 consecutive Sunday weekly-suggestion emails arrive with ≥ 1 sensible context/earnings adjustment and a legible `context_note` (earliest 2026-06-07)

*(Note: 4.6 is the auto-trade workstream inside Phase 4 (`v0.4.6.*` promotion tags). 4.5 produces the Weekly Market Context as informational content for the Friday email; 4.7 is what makes that context — plus a structured earnings calendar and a sentiment signal — actually drive Sunday's order **sizing**. Read together, 4.5 → 4.7 is "context is information" → "context is risk-managed sizing.")*

**What shipped:**

- `graphs/suggestion_review.py` — new `context_adjust` node spliced between `reason` and `critic`; three sub-passes (deterministic earnings gate → bounded Sonnet narrative multiplier → Python clamp/floor/drop/reanchor) with rationale re-keying after sub-1-share drops ✓
- `services/earnings.py` — `EarningsClient` Protocol + `FinnhubEarningsClient` (free-tier `earnings_calendar` endpoint) + `FakeEarningsClient` + factory; structured `{ticker: date}` chosen over Tavily's free-text `forward_events` for the deterministic gate ✓
- `services/sentiment.py` — `SentimentClient` Protocol + `FinnhubCNNSentimentClient` (VIX via Finnhub, CNN Fear & Greed via public endpoint) + `FakeSentimentClient` + factory; explicit "never raise on failure" contract so a CNN endpoint change cannot block the Friday job ✓
- `prompts/context_size_v1.txt` + `context_size_v2.txt` — v1: bounded multiplier + `prefer_anchor` must be a known scored level for the correct side; v2 (opt-in): adds VIX/Fear-&-Greed sizing rules referencing `asset_classes[ticker]` symbolically (no hardcoded ticker lists in the prompt) ✓
- `prompts/suggestion_critic_v2.txt` — rule 6 ("Sizing already adjusted — RESPECT these. Only override if the adjustment created a NEW problem") so the critic doesn't silently undo a defensive shrink or rubber-stamp a risky upsize ✓
- `weekly_market_context` table + `persist_weekly_context()` / `load_latest_weekly_context()` — 4.5 deliberately discarded Tavily synthesis; 4.7 persists the `WeeklyMarketContext` (now also carrying `vix`, `fear_greed_score`, `fear_greed_label`) so Sunday's engine reads Friday's narrative + sentiment. Append-only event table (convention #9); `context_max_age_days=4` rejects stale rows; staleness check lives in the SQL `WHERE` to dodge the SQLite naive-datetime trap ✓
- `order_suggestion` audit columns (`base_qty`, `size_factor`, `context_note`) with the invariant: `base_qty IS NULL` = `context_adjust` never ran (pre-4.7 row or fresh-context unavailable); `base_qty IS NOT NULL` = node ran, even when neutral ✓
- Weekly suggestions email — `(base N · ×F)` badge on adjusted qty + `context_note` line in both HTML and text templates so a reader can see *why* a qty changed at a glance ✓
- `config/targets.yaml` — per-ticker `asset_class: index_etf | leveraged_etf | equity` (default `"equity"`, unknown values warn + coerce); the Sonnet payload now carries `asset_classes` so v2 prompt rules apply symbolically, not by hardcoded ticker name. Adding new ETFs no longer requires editing a prompt file ✓
- Direction-aware tick-size rounding — `_floor2dp` (buy limits, never overpay) and `_ceil2dp` (sell limits, never undersell) replace bankers'-rounded `round(..., 2)` at the Alpaca adapter and at every `context_adjust` reanchor site. The DB retains full-precision scored levels for audit; only the value sent to the broker is rounded ✓
- Kill-switch policy split — new `BrokerValidationError` domain exception in `brokers/base.py`; Alpaca HTTP 422-class rejections (sub-penny, wash-sale) now map to `BrokerValidationError` and are caught separately in `auto_trade.py` as per-suggestion skip + continue (system stays LIVE). Only unexpected `Exception` still trips the kill switch. One malformed price no longer orphans every remaining accepted suggestion for the day ✓
- `UTCDateTime` TypeDecorator — all 22 `DateTime(timezone=True)` columns migrated; reattaches `tzinfo=UTC` on load so SQLite-naive reads can no longer poison `now(UTC) - row.ts` comparisons anywhere in the codebase. Fixes the *class* of bug, not just the one site that surfaced it ✓
- Prompt-version safety — `@field_validator` on all 4 version settings strips a leading `v`; format strings add the canonical `v`. The same string-concat bug that produced `weekly_context_vv1.txt` in 4.5 and `context_size_vv1.txt` in 4.7 is now structurally impossible ✓
- 38 new/extended tests (5 earnings + 12 context_adjust + 8 sentiment + 1 UTCDateTime round-trip + 3 config + 2 auto_trade + post-deploy extensions); total 298 (up from 260 at Phase 4.5 close) ✓
- ADR-0021 (bounded context exception to ADR-0020; carved LLM size-multiplier exception; Finnhub calendar over Tavily forward-events) + ADR-0022 (SentimentClient Protocol; CNN F&G fragility contract; ETF classification authoritative in `targets.yaml`) ✓

**Bugs caught and fixed:**

Eleven bugs during initial build (9 caught in implementation review, 2 post-deploy: `context_size_vv1.txt` filename recurrence, and a sub-penny limit price that fired the kill switch and orphaned five accepted suggestions). A subsequent code review then surfaced 7 systemic issues — the datetime fix was site-local rather than type-systemic; tick-size rounding was Alpaca-only and direction-blind; the kill switch couldn't distinguish "malformed request" from "real risk-control breach"; VIX/F&G were bare private functions with no Protocol or test seam; ETF lists were hardcoded in the prompt; an over-eager initial fix had broken the `base_qty IS NULL` audit invariant; v2 was set as default without a soak cycle. All seven were resolved in a single pass before the paper-dry-run soak began. Full breakdown in §2b of `phase_4_7_completion.md`.

**Phase 4.7 cleanup status:**

| Item | Status | Notes |
|---|---|---|
| Code complete | ✅ Done | 2026-05-26 |
| 298 tests green | ✅ Done | up from 260 at Phase 4.5 close |
| `ruff` + `mypy` clean | ✅ Done | No new errors on Phase 4.7 files |
| ADR-0021 written | ✅ Done | Accepted |
| ADR-0022 written | ✅ Done | Accepted (post-deploy code review) |
| CLAUDE.md + README + `.env.example` updated | ✅ Done | Bounded Tavily exception; gotchas 24–26; new env vars |
| `alembic upgrade head` applied in production | ⏳ Pending | First boot of v0.4.7.0 — two new migrations |
| First Friday `weekly_market_context` row with non-null `vix` + `fear_greed_score` | ⏳ Pending | Earliest 2026-05-29 |
| First Sunday suggestions email with ≥ 1 `(base · ×F)` badge + `context_note` | ⏳ Pending | Earliest 2026-05-31 |
| Second Sunday email — consistency check | ⏳ Pending | Earliest 2026-06-07 → enables tag |
| Live earnings-gate verification (real ticker with confirmed upcoming earnings) | ⏳ Pending | Soak-window opportunistic |
| Negative-test: `TAVILY_API_KEY=""` → narrative + sentiment skipped, earnings gate still applies, no crash | ⏳ Pending | Manual check |
| Negative-test: `FINNHUB_API_KEY=""` → earnings + VIX disabled with WARNING, no crash | ⏳ Pending | Manual check |
| v2 prompt opt-in soak (`CONTEXT_ADJUST_PROMPT_VERSION=2`) | ⏳ Pending | Default stays `"1"` until v2 has run one observed Sunday cycle |
| Spot-check: CNN F&G `urlopen` has an explicit `timeout=…` — ADR-0022's "never block the Friday job" contract holds at the code level, not just at the catch-Exception level | ⏳ Pending | One-line verification |
| Resolution note for sug-23/AAPL (left `pending` post Bug 11 recovery) | ⏳ Pending | Either re-place on next auto-trade run or document why skipped |

**Hard guardrails enforced (suggest-only invariant preserved):**

`context_adjust` may only **scale `qty`** within `[CONTEXT_SIZE_MIN, CONTEXT_SIZE_MAX]` and **re-pick among existing scored S/R anchors** for the same ticker and side. It cannot originate a ticker, flip a side, or invent a price. Sonnet's `size_multiplier` is Python-clamped after the call; `prefer_anchor` is validated against `scored_levels` (wrong-side or unknown methods are silently rejected); a buy can only re-anchor to a support and a sell to a resistance. The price ultimately sent to the broker is `_floor2dp` for buys and `_ceil2dp` for sells, so float precision in scored levels can never cause an overpay or undersell. ADR-0021 records why these bounds keep the feature on the suggest-only side of the regulated-advice line even with full bidirectional influence.

**Why v2 prompt is opt-in, not default:** the v2 prompt's VIX/Fear-&-Greed sizing rules — especially "VIX > 35 (crisis) → upsize to `bounds.max`" on non-leveraged index ETFs — encode a deliberate **buy-the-dip / value-investor prior**. That stance is fine but consequential, and v2 had not completed a full Sunday soak when the post-deploy review landed. Default is `CONTEXT_ADJUST_PROMPT_VERSION=1` (narrative-only); flip to `2` after one observed Sunday cycle of v2 behaviour matches expectations. The same opt-in discipline applies to any future prompt version.

**Why context_adjust runs *before* critic in the review graph:** the critic already reviews drafts as a *set*, so combined-cash-floor and over-concentration problems introduced by an upsize land naturally in its hands. Doing the cash check inside `context_adjust` would duplicate logic and break single-responsibility. The critic prompt v2 explicitly tells it to **respect** prior adjustments and only override when the adjustment created a *new* problem.

**Why kept separate from Phase 4.5:** 4.5 produces the synthesis; 4.7 *consumes* it. They have different failure modes (Tavily outage degrades 4.5; Finnhub outage or stale-context degrades 4.7 independently) and different release gates (4.5 needs 2 Friday emails with non-empty content; 4.7 needs 2 Sunday emails with a visible, sensible adjustment). Bundling them would have coupled the 4.5 soak to the 4.7 implementation, slowing both.

**ADRs:** ADR-0021 (context-aware weekly order sizing; bounded exception to ADR-0020; LLM size-multiplier carve-out; Finnhub calendar choice), ADR-0022 (SentimentClient Protocol; CNN Fear & Greed fragility contract; ETF classification authoritative in `targets.yaml`) ✓

See `phase_4_7_guide.md` for the step-by-step build and `phase_4_7_completion.md` (§2a for VIX/F&G; §2b for the post-deploy code review's 7 systemic fixes) for the as-built record.

### Phase 4.8 — Weekly Order Activity Summary + lifecycle hardening ⚙️ Code complete (2026-05-28); post-review fixes shipped 2026-05-29; tag `v0.4.8.0` deferred until 2 consecutive Friday weekly-review emails arrive with the Order Activity section populated and every headline cross-checked against hand-written SQL (earliest 2026-06-05)

*(Phase 4.8 is the final Phase 4 slot. After this, the Phase 4 family is complete — 4 → 4.5 → 4.6 → 4.7 → 4.8 — and Phase 5 multi-tenant productization begins.)*

**What shipped — measurement section:**

- `services/weekly_review_metrics.py` — 5 frozen dataclasses (`OrderFunnel`, `OrderFlow`, `AllocationDriftRow`, `PerTickerWeekRow`, `WeekTrendRow`) + 5 pure compute functions; no ORM rows cross the session boundary ✓
- 4 SQL files in `src/investor/sql/` (`funnel_counts`, `order_flow`, `alloc_drift`, `per_ticker_breakdown`) — `COUNT(DISTINCT suggestion_id)` per state to prevent GTC-partial-fill double-counting; `COALESCE` guards on `avg_fill_price` for pre-reconciliation rows; `NULLIF(..., 0)` guards zero-equity; `MAX(snapshot_date) ≤ :date` fallbacks for missing snapshots ✓
- Friday email gains an **Order Activity** section between weekly-suggestions performance and auto-trade soak status — funnel table, dollar-flow table, allocation-drift table (green/red drift_pp + "→ closer/farther"), per-ticker breakdown, 4-week trend strip; DRY_RUN line hidden when zero; holiday and mid-week-targets footnotes conditional ✓
- Weekday guard — manual triggers on non-Friday days raise rather than render a half-week's data ✓
- Settings: `weekly_review_trend_weeks=4`, `weekly_review_breakdown_top_n=20` — no env-var required ✓
- ADR-0023 (allocation drift over trade-attributable fill-rate fiction; live queries over a materialised cache at single-user scale; honest `accepted_not_routed` bucket rather than position-delta inference) ✓

**What shipped — lifecycle bug fixes (8 issues found in a post-implementation read of the suggestion → order → execution data flow):**

- **B2 (HIGH)** — Rule 1 of reconciliation crashed on `sug-N-rN` retry IDs (generated by `_next_client_order_id()` after a `broker_cancelled` re-place), leaving re-placed orders untracked. Fixed via `_parse_suggestion_id` regex helper shared between the generator and parser.
- **B1 (MEDIUM)** — Manual broker cancellations were silently skipped by `reconcile_activities` (no `filled_at`). New `sync_open_order_statuses()` polls open executions via a single batch `list_orders(status="closed")` call (added to the `BrokerAdapter` Protocol; implemented for both Alpaca and Moomoo) and flips terminal-status rows to `broker_cancelled`.
- **B3 (MEDIUM)** — Partial-fill activities prematurely flipped the suggestion to `filled` while the GTC remainder was still live. Now gated on `activity.status == "filled"`.
- **B5 (MEDIUM)** — Mid-week target changes left accepted suggestions in `accepted` for now-removed tickers; auto-trade would route them next day. `load_targets_into_db()` now expires those suggestions *and* calls `adapter.cancel_order()` on any linked live GTC, closing the money-at-risk window between target edit and the morning sweep.
- **B6 (MEDIUM)** — Expiry sweep called `cancel_order()` but never updated `exec.status`. Now sets `broker_cancelled` *only* after `get_order()` confirms cancellation; if the broker says "already filled" the row is left for reconciliation to resolve correctly.
- **G1/G2/G3 (LOW)** — pending-past-`expires_at` rows display "(expires Mon)" in the audit; reset endpoint gained `side: buy | sell | all` and a canonical `/admin/reset-week-suggestions` route (old `-buy-` URL retained as backward-compat alias); trend strip's "filled" column now includes partial fills and the header reflects it.

**What shipped — structural safety net:**

- Expiry sweep moved from **16:20 ET → 09:00 ET Mon-Fri**. Closes the documented Monday-morning dual-GTC race where last week's GTC sat live from 09:35 ET (when auto-trade fired) until 16:20 ET.
- **`_check_stale_live_order` guard in `auto_trade.py`** — refuses to place a new order while *any* `accepted_for_routing` execution exists for the same ticker from a different suggestion. This makes "never two live orders for the same ticker" a *structural* property, enforced at placement time, rather than a *timing* property that depends on APScheduler delivering the sweep before 09:35 ET (which a missed sweep, deploy window, or NTP skew can break).

**Post-review remediation:** a second-pass code review surfaced 7 gaps in the initial lifecycle bug fixes — B5 was flipping status without cancelling the linked broker order; B6 was setting `broker_cancelled` unconditionally after a cancel API call, ignoring the "broker says already filled" race; the scheduler-move alone wasn't a structural property; B2's inline `.split("-r")[0]` was brittle to ID-format changes; B1 was polling O(N) per row instead of batching; the reset endpoint URL still said "buy"; the trend column header didn't reflect the partials inclusion. All 7 closed in a follow-up commit; details in `phase_4_8_post_review_fixes.md`.

**Test growth:** 298 (4.7 close) → 305 (post-4.7 misc) → 315 (4.8 metrics, +10 smoke) → 334 (lifecycle bug fixes, +19) → **342** (post-review fixes, +8).

**Phase 4.8 cleanup status:**

| Item | Status | Notes |
|---|---|---|
| Code complete | ✅ Done | 2026-05-28; post-review fixes 2026-05-29 |
| 342 tests green | ✅ Done | 1 pre-existing flaky test (`test_weekday_guard_raises_on_wednesday` — module-reload + datetime-patch interaction; unrelated to 4.8 work) |
| `ruff` + `mypy` clean | ✅ Done | No new errors on 4.8 files |
| ADR-0023 written | ✅ Done | Accepted |
| CLAUDE.md + README updated | ✅ Done | Order Activity section, two new settings, gotcha on operations-week / GTC-cross-week lag |
| `list_orders` added to `BrokerAdapter` Protocol; Alpaca + Moomoo implementations | ✅ Done | Moomoo implementation ships ahead of Moomoo go-live so the parallel-run soak can exercise it |
| Structural stale-live-order guard live in `auto_trade.py` | ✅ Done | Independent of scheduler timing |
| First live Friday email with Order Activity section | ⏳ Pending | Earliest 2026-05-29 |
| Second consecutive Friday email — consistency check | ⏳ Pending | Earliest 2026-06-05 → enables tag |
| `accepted_not_routed` label correctly reads "not yet routed" in DRY_RUN (not "presumed manual") | ⏳ Pending | Confirm in first soak email |
| Every headline cross-checked against hand-written SQL for one Friday | ⏳ Pending | The only way to catch a quietly-wrong query in production |
| 09:00 ET Monday expiry sweep verified pre-flight to 09:35 ET auto-trade | ⏳ Pending | Soak-window opportunistic |
| Stale-live-order guard exercised in real auto-trade run | ⏳ Pending | Soak-window opportunistic |
| Trend strip populated with 4 weeks of LIVE data | ⏳ Long-tail | First three Fridays in LIVE mode show fewer columns by design |
| `src/investor/sql/*.sql` vs `queries.py` two-sources-of-truth question resolved or wrapper pattern documented | ⏳ Open | Carried from prior review |
| Flaky `test_weekday_guard_raises_on_wednesday` fixed | ⏳ Open | One-line follow-up so CI runs 342/342, not 341/342 with a known-flake |
| Carried from 4.7: sug-23/AAPL pending resolution; CNN F&G `urlopen` explicit timeout | ⏳ Open | Fold into the same pre-tag cleanup pass so they don't bleed into Phase 5 |
| Trend column header micro-nit ("Filled+∂" vs "Filled (incl. partial)") | ⏳ Optional | Defensible as-is; ∂ is unambiguous to a developer, slightly opaque to a casual reader |

**Hard guardrails preserved:**

The Order Activity section is backward-looking reporting only — nothing in it feeds back into `generate_suggestions()`, `context_adjust`, `auto_trade`, or any broker adapter. Suggest-only invariant unaffected. The honest-accounting `accepted_not_routed` bucket surfaces the "accepted but no LIVE `order_execution` row" gap rather than guessing — manual broker placements remain invisible by design (ADR-0023 records the rejected alternative of position-delta inference). No new schema; no new mutable state; the upgrade trigger to a `weekly_metrics_cache` table is documented (any single metric query crossing 500ms at email-send time) but not adopted.

**Why drift over trade-attributable fill-rate:** `$ filled ÷ $ suggested` looks meaningful and breaks the moment a suggestion fills partially, gets re-placed after `broker_cancelled`, fills next week against a GTC order, or is placed manually. Drift just measures what the portfolio did — which is what a long-term investor cares about. ADR-0023 records the rejected alternative explicitly so the next agent doesn't add the seductive metric back.

**Why the structural guard rather than tightening the scheduler:** APScheduler delays (deploys, container restarts, NTP skew) make any timing-only fix probabilistic. The 30-minute misfire grace bounds the failure but doesn't eliminate it. Adding `_check_stale_live_order` in `auto_trade.py` upgrades "never two live orders for the same ticker" from a property of the cron timing to a property of the placement code itself — verifiable at every run, not just verifiable in the absence of operational incidents.

**Why kept separate from 4.6 (auto-trade):** 4.6 is about *placing* orders; 4.8 is about *reporting* on them. 4.8 reads `order_execution` rows that 4.6 writes; bundling the read and write workstreams would have coupled the 4.6 LIVE-soak progression to a reporting feature that doesn't gate it. The lifecycle bug fixes that crossed both (B1, B5, B6) landed naturally during 4.8 because that's when reading the data flow as a whole surfaced them.

**ADRs:** ADR-0023 (weekly order activity metrics — allocation drift over fill-rate fiction; live queries at single-user scale; honest `accepted_not_routed` bucket) ✓

See `phase_4_8_guide.md` for the step-by-step build, `phase_4_8_completion.md` for the as-built record (Phase 4.8 features + 8 lifecycle bugs + scheduler fix), and `phase_4_8_post_review_fixes.md` for the 7 follow-up remediations.

### Phase 4.9a — Multi-broker plumbing + per-broker reports 📋 Planned (guide: `phase_4_9a_guide.md`); kicked off when Phase 4.8 tag is observably stable and the 4.8 cleanup-table items closed

*(Phase 4.9 is the bridge between the solo single-broker product and Phase 5 multi-tenancy. It splits into 4.9a — multi-broker plumbing — and 4.9b — household targets + rebalance reviews. Phase 5a then inherits a much cleaner data model because `broker_account_id` is already on every per-account table by the time `user_id` lands.)*

**What will ship:**

- `broker_account` becomes a real per-account table — multiple rows per user, each with `id` (UUID), `broker`, `nickname`, `is_active`, time-versioned `cash_usd` / `equity_usd`, JSON `connection_config`. Soft-delete only (convention #9 close-and-insert on the time-versioned fields; the row itself is never hard-deleted because all historical positions/suggestions/executions point at it).
- `broker_account_id` migrated onto every per-account table (`target_allocation`, `positions_snapshot`, `order_suggestion`, `order_execution`); every existing `UniqueConstraint` rebuilt with `broker_account_id` as the leading column; Jane's existing data backfilled to her Alpaca UUID with a verification SQL (`SELECT broker_account_id, COUNT(*) FROM order_suggestion GROUP BY 1` returns exactly one row pre-migration row count).
- `auto_trade_state` table — per-broker auto-trade mode (`OFF` | `DRY_RUN` | `LIVE`); the Phase 4.6 OFF → DRY_RUN → LIVE-paper → LIVE-live soak ladder now applies *per broker*. Promoting Alpaca to LIVE does not affect Moomoo, IBKR, or Tiger. Phase 4.8's structural `_check_stale_live_order` guard rescopes to `(broker_account_id, ticker)` — two live orders for the same ticker across different brokers is allowed; two on the same broker is still what the guard prevents.
- Broker roster: 4.9a ships **Alpaca + Moomoo** (the latter exercised live on a real funded account during the 2026-05-31 smoke). IBKR + Tiger adapters were originally bundled into 4.9a but were paused mid-build and moved to **Phase 4.9c** (a sibling sub-phase to 4.9b — can ship in parallel; see `phase_4_9c_guide.md`).
- Per-broker daily and weekly emails — `[{nickname}] Daily report for YYYY-MM-DD` subject lines; N emails per cron run with N active brokers. The consolidated view is deferred to 4.9b; expect N daily + N weekly emails per week until then.
- `targets.yaml` → `data/targets/<broker_account_id>.yaml` per broker; `load_targets_into_db()` parameterised by `broker_account_id`. Phase 5a finishes the move from YAML files to DB rows authored via the dashboard.
- Per-broker reconciliation and expiry sweep — every Phase 4.8 fix (B1's batch `list_orders`, B5's cancel-on-target-change cascade, B6's verify-before-broker-cancelled, the structural stale-live-order guard) applied per broker_account.
- ADR-0024 (multi-broker single-user data model — `broker_account_id` partitioning rationale, news/levels/context staying user-level, soft-delete-only policy, per-broker soak ladder semantics) ✅ written. ADRs for IBKR and Tiger move with their adapters to Phase 4.9c; the *numbers* originally reserved (0025, 0026) are now taken by code-repo ADRs (donut chart, SQLite WAL fix — see the "Architecture Decision Records" section at the end of this document), so 4.9c's ADRs will be assigned at resumption (likely **0037 IBKR, 0038 Tiger**).

**Expected time budget:** 2–3 weeks. IBKR adapter is the longest single sub-task (~1 week) due to the host-side Gateway dependency and the persistent-socket reconnect machinery. Tiger is ~3–5 days. The data-model migration is ~1.5 days; per-broker report wiring is mechanical once partitioning is clean.

**Depends on:**
- Phase 4.8 tagged with all carried-over cleanup items closed (queries duplication, flaky weekday-guard test, sug-23/AAPL pending resolution, CNN F&G `urlopen` timeout).
- (Originally noted: "decide up-front whether to onboard IBKR + Tiger together with Alpaca + Moomoo, or stage them in." Resolved in flight — IBKR + Tiger were paused mid-build and moved to Phase 4.9c. 4.9a shipped with Alpaca + Moomoo only; the multi-broker model is exercised at 2 brokers, sufficient for the 4.9b household view to land.)

**What 4.9a deliberately doesn't include:**
- Household target allocation, consolidated summary emails, the `email_aggregation` toggle — all 4.9b.
- Funds-added detection, quarterly/annual review crons, magic-link target-edit guardrails — also 4.9b.
- **IBKR or Tiger auto-trade LIVE.** Each new broker's LIVE promotion needs its own Phase 4.6 OFF → DRY_RUN → LIVE soak ladder; that's calendar time, not engineering time. 4.9a ships them as suggest-only readers and draft-submitters; LIVE per broker is a separate later workstream after the read paths are observed.
- Cross-broker order routing — the user picks the broker for each manual order; the system never moves cash, positions, or orders between brokers.
- Multi-currency native targets, lot-level cost basis tracking, options/futures/crypto in `get_positions` — all Phase 6+ at earliest.

See `phase_4_9a_guide.md` for the step-by-step build. Tag: `v0.4.9a.0`.

### Phase 4.9b — Household targets + consolidated summary + rebalance reviews 🅿️ Parked (2026-06-09); resume after the post-4.9a soak window (target: a few weeks of clean operational signal)

**Why parked, deliberately:** the post-4.9a stretch (2026-06-03 → 06-09; recorded in `post_4_9a_changes.md`) landed a substantial batch of correctness fixes that were caught by *operational observation*, not by tests — direction-aware mover tiers + weekly fresh-start (`1c38fd6`), drift-basis = total-equity (`e5f5a76`), single-snapshot dedup (`a4f9418`), late-GTC reconciliation window (`56438b6`), bars split-adjustment + 50%-distance filter (`85053ca`), CNN sentiment fetch headers + Finnhub-VIX fallback (`acd9a5c`), un-accept path with new `cancelled` terminal status (`72d0d5c…0b48307`), plus the LLM-call-path tuning round (`61077a0`) and the shared email design system (`1aa36cd`). Each of these has its own subtle steady-state behaviour that's worth watching for a few weeks before adding new surface area on top. Soaking the current solution longer is more valuable right now than rushing the household summary in. Resume target: when the soak produces a clean cycle with no new bug-class surfaced *and* the candidate ADRs below have been promoted into the Architecture Decision Records section at the end of this document (✅ done 2026-06-09 / renumbered 2026-06-18 — see ADRs 0025–0032; the §9 snapshot-one-ts-per-batch contract is the remaining un-written candidate at reserved slot 0033).

**Pending ADR promotion (from `post_4_9a_changes.md` §"Candidate ADRs / gotchas"):** split-adjusted bars + re-backfill procedure (highest priority — silently changes a data-semantics assumption); VIX/F&G from CNN `graphdata` with browser headers (fragility contract); shared email components (`_components.html.j2` + `_sentiment.html.j2`); movers tiers direction-aware + ISO-week reset; suggestion status `cancelled` terminal. These were grouped as candidates by the agent; promoting at least the bars one before tag avoids any future reader hitting the pre/post-split bar mismatch without context.

**Original planned scope follows — for resumption later:**



**What will ship:**

- `household_target_allocation` table — *optional* reference target spanning all of a user's brokers (per-ticker `target_pct` of total household equity, `asset_class` carrying from Phase 4.7, time-versioned with the close-and-insert pattern, optional advisory `preferred_broker_account_id`). Per-broker targets remain authoritative; the household target is an aspirational reference. ADR-0027 is the most consequential design call in 4.9b.
- `services/household_summary.py` — `HouseholdSnapshot` frozen dataclass aggregating per-broker `positions_snapshot` + cash; household drift computation against the explicit household target if declared, or against the *implied* household target (per-broker target sum weighted by per-broker equity) otherwise. The email footnote names the source so the reader knows which model is in play.
- Consolidated daily + weekly review emails — household header (total equity, total cash, # active brokers, household drift table top-N) + per-broker sections + footer. Phase 4.5 Weekly Market Context and Phase 4.8 Order Activity sections now aggregate across brokers (the funnel sums; per-broker breakdown becomes a new bottom section).
- `user_settings.email_aggregation` — `per_broker` (4.9a default), `consolidated` (4.9b default when `household_target_allocation` is declared), `both` (power user — N+1 emails). The system surfaces both views rather than forcing one.
- Funds-added detection per broker (`jobs/funds_detection.py`, daily 18:00 ET strict) — `(today's equity change) − (market moves + realised PnL) > funds_detection_threshold_usd` (default $500); cross-broker transfers surface as *two* events with a header note "consider whether these are a single transfer" rather than silently merging. `funds_event` append-only.
- Quarterly review cron (first trading day Jan / Apr / Jul / Oct, 06:00 ET); annual review cron (first trading day January, 06:00 ET, with a tax-loss-harvesting hint section using approximate cost basis + explicit caveat); tax-year-end reminder December 20 (US users only, gated on `tax_jurisdiction = "US"`). All three are *prompts*, never actions — the suggest-only product principle extends from order suggestions to target edits.
- YAML edit guardrails + magic-link confirmation — pre-commit hook computes per-ticker shift; if `max(|shift|) > target_edit_magic_link_threshold_pct` (default 10) the commit is held, a magic-link email is sent, and clicking commits the change and writes a `target_change_event` row. Phase 4.8's B5 cascade fires on either path: any accepted suggestion for a removed ticker is expired *and* its linked GTC cancelled per broker. The magic-link signing uses a distinct namespace (`targets-confirm-v1`) from any future auth magic-link to prevent confusion (pitfall called out in the Phase 5 guide).
- `target_change_event` append-only audit table — `broker_account_id` (NULL = household-level edit), `source` (`yaml_direct` | `yaml_magic_link` | `dashboard` once 5b lands), diff JSON, `confirmed_by` (`auto` for small edits, `magic_link:<email>` for confirmed large ones).
- ADRs to write at resumption — three planned topics (household target as optional reference; per-broker primary, rejected alternative of household-primary that would imply cross-broker allocation = closer to advice / funds-added detection heuristic + cross-broker transfer surfacing + wash-sale-not-handled caveat / email aggregation toggle rationale). The *numbers* originally reserved (0027, 0028, 0029) are now taken by post-4.9a hardening ADRs (see the "Architecture Decision Records" section at the end of this document), so these will be assigned at resumption (likely **0034, 0035, 0036** after the 2026-06-18 renumber).

**Expected time budget:** 1–2 weeks. Most of the work is query + email rendering on top of the schema 4.9a already provides; the rebalance crons are mechanical; the magic-link plumbing reuses prior infrastructure (and is the same pattern Phase 5b's dashboard edit path will use).

**Depends on:** Phase 4.9a (`v0.4.9a.0`) tagged with all 11 smoke rows green.

**What 4.9b deliberately doesn't include:**
- Dashboard editor for household targets — Phase 5b. In 4.9b the household target is edited via `data/household_targets.yaml` with the same magic-link guardrail as per-broker yamls.
- Cross-broker order routing — household drift surfaces "buy AAPL" with the advisory `preferred_broker_account_id` (if declared); the user picks the broker and places the order manually.
- Lot-level tax-loss harvesting suggestions — approximate cost basis with an explicit caveat is enough at this scope; precise lot-level TLH is Phase 6+ and probably never (most brokers do it at their UI).
- Real-time / intraday consolidated view, risk parity / mean-variance / model portfolios, cross-broker auto-rebalancing — all Phase 6+ at earliest, several of them never (they cross into regulated advice).
- Quarterly / annual review *action* automation — a future "approve all recommendations" button is suggest-only-line territory and needs an ADR before it ships.

See `phase_4_9b_guide.md` for the step-by-step build. Tag: `v0.4.9b.0`; the umbrella `v0.4.9.0` is set after 4.9a + 4.9b + 4.9c are all observably stable (or after whichever subset has been completed if Jane elects to ship without all three).

### Phase 4.9c — IBKR + Tiger adapters (suggest-only) 🅿️ Parked (2026-06-09); resume alongside or after 4.9b once the post-4.9a soak window has produced a clean operational cycle

**Why parked, alongside 4.9b:** the post-4.9a hardening batch (see 4.9b parking note above) is the operational behaviour Jane wants to soak. Adding two new broker adapters — even read-only / suggest-only ones — expands the surface that has to behave correctly during the soak window: two more `get_positions` paths, two more host-side dependencies (IB Gateway, Tiger RSA-signed REST), two more code paths that could produce a confusing email or a stuck reconciliation row, and four daily emails / Sunday weekly emails instead of two. Resume target: alongside or just after 4.9b restarts, once a clean operational cycle has confirmed the post-4.9a fixes are steady-state.

**Original planned scope follows — for resumption later:**



*(4.9c is the **sibling** of 4.9b. 4.9b layers household summary on top of the per-broker plumbing; 4.9c extends the broker roster from Alpaca + Moomoo to Alpaca + Moomoo + IBKR + Tiger. Neither depends on the other — they can ship in either order or in parallel. Originally bundled into 4.9a; paused mid-build when the focused scope was "Foundation + Moomoo"; resumed here as a standalone sub-phase.)*

**What will ship:**

- **`IBKRAdapter`** via `ib_insync` against IB Gateway (or TWS) — persistent socket model, same host-side dependency pattern as Moomoo's OpenD; daily Gateway restart at 23:45 ET handled by reconnect-on-error; `client_id` uniqueness convention documented in `connection_config` per account. Full `BrokerAdapter` surface (`get_positions`, `get_account`, `submit_order_draft`, `submit_order`, `cancel_order`, `get_order`, `list_orders`). The `submit_order` LIVE path ships but is gated by `auto_trade_state.mode='OFF'` by default — LIVE soak deferred to its own Phase 4.6-style workstream after read paths are observed.
- **`TigerAdapter`** via `tigeropen` — REST with RSA-signed requests; regional account types TBSG/TBAU/TBKR/TBHK; FX conversion to USD at the adapter boundary for non-USD accounts (TBAU = AUD, TBHK = HKD, TBSG = SGD or USD) via dual `cash_native` / `cash_usd_at_snapshot` storage; per-position currency labels consistent with the convention 4.9a's post-smoke fixes established (commit `391d062`).
- Onboarding pre-flight docs per broker — IB Gateway install + auth, Tiger RSA key generation, `connection_config` examples per broker.
- Suggest-only across both — each new broker registers with `auto_trade_state.mode='OFF'` by default. `submit_order_draft` works; `submit_order` is wired but gated; per-broker LIVE soak is its own Phase 4.6-style workstream.
- Unit + integration tests against paper accounts (gated by env var so CI doesn't need credentials); mock adapters for CI; smoke tests on real IBKR paper and Tiger paper or sandbox.
- The 4.9a post-smoke lessons re-applied to both new adapters: `_floor2dp` / `_ceil2dp` direction-aware rounding at `submit_order`; reconciliation by `broker_order_id` (not broker-string); currency labelling on positions; `secType == "STK"` filter; reconnect-on-error wrapper for the persistent-socket adapter.
- ADR-0025 (IBKR via `ib_insync` against IB Gateway — `ib_insync` over raw `ibapi` for ergonomics + Protocol fit; Gateway over Web API for full GTC / extended-hours surface; `client_id` uniqueness; reconnect pattern) and ADR-0026 (Tiger via `tigeropen`; FX-at-snapshot convention with dual native/USD storage; regional account currency configured per account, not inferred from region code; RSA key path in `connection_config`, never key contents).

**Expected time budget:** ~1.5–2 weeks. IBKR is the bigger chunk (~1 week) due to the host-side Gateway dependency and persistent-socket lifecycle. Tiger is ~3–5 days. Per-broker config wiring is mechanical — the multi-broker plumbing already exists from 4.9a's Stages A–C, so 4.9c is purely "add two new cases to `make_account_adapter` and harden them through smoke."

**Depends on:** Phase 4.9a (`v0.4.9a.0`) tagged. Does *not* depend on 4.9b — the household summary works fine with Alpaca + Moomoo only; the new adapters slot in transparently when they land.

**What 4.9c deliberately doesn't include:**
- **IBKR or Tiger auto-trade LIVE.** Each new broker's LIVE promotion follows the Phase 4.6 OFF → DRY_RUN → LIVE-paper → 28-day LIVE-live ladder — calendar time, not engineering time. 4.9c ships them as suggest-only.
- **IBKR Web API path** (rejected per ADR-0025 for limited surface — no GTC, partial extended-hours, narrower asset universe; revisit if Gateway becomes a hard ops burden).
- **Tiger native-currency target support.** Targets remain USD; non-USD accounts convert at snapshot time with native-currency labels in display. Native-currency targets are Phase 6+.
- **IBKR multi-currency account handling.** 4.9c gates IBKR onboarding to `currency == "USD"`. IBKR Australia / IBKR HK are a future enhancement.
- **Schwab, Fidelity, Robinhood, Webull, Trading 212, Saxo.** Wait for user demand. Each adapter is ~1 week of focused work behind the existing Protocol; the pattern is now well-established.
- Options / futures / crypto in `get_positions` (filtered to STK only — same as 4.9a's Moomoo handling).

See `phase_4_9c_guide.md` for the step-by-step build. Tag: `v0.4.9c.0`.

### Phase 5 — Productization + target adjustment & rebalance reviews (3–4 weeks)
- Add React dashboard (Vite + Tailwind) reading the same FastAPI.
- Auth (Clerk or Supabase).
- Multi-tenant data model: every row gets `user_id`.
- Split storage: **Postgres** for OLTP/auth/user rows, **DuckDB** (or MotherDuck in cloud) for per-user analytics/bars.
- Encrypted per-user broker credentials (envelope-encrypted with a KMS key or `cryptography.fernet` with a rotating master key).
- Pluggable per-user broker adapter — user picks Alpaca, Moomoo, or IBKR at connect time.
- **Target adjustment & rebalance reviews** — moved to Phase 4.9b (above). Phase 5 inherits the `target_change_event` table, the funds-detection heuristic, the quarterly / annual / tax-year-end cron schedule, and the magic-link confirmation plumbing. Phase 5b's dashboard targets-edit page is the multi-tenant face of the same machinery: same threshold, same `target_change_event` row (with `source="dashboard"`), same B5 cascade on removed tickers. The "previously Phase 4.5, folded in" history is now: lived in the Phase 4.5 stub, moved to Phase 5 in the prior restructure, moved to Phase 4.9b once multi-broker plumbing made it the natural home.
- **Mandatory pre-implementation cleanup — ADR renumbering collision.** The Phase 5 guide (`phase_5_guide.md`) currently allocates **ADRs 0024–0031** to Phase 5 work, but the live ADR sequence is now (post-2026-06-18 renumber): ADR-0024 shipped with Phase 4.9a; ADR-0025 = donut chart; ADR-0026 = SQLite WAL fix; ADRs 0027–0032 = post-4.9a hardening batch (see the Architecture Decision Records section at the end of this document); ADR-0033 reserved for §9 snapshot-one-ts-per-batch contract (TBD); ADRs 0034–0036 reserved for 4.9b resumption; ADRs 0037–0038 reserved for 4.9c resumption. Before any Phase 5 implementation begins, renumber the Phase 5 guide's ADRs to **0039+** so the numbering stays sequential and a future reader doesn't encounter competing ADR numbers across phases. Pure docs work — one find-and-replace pass through `phase_5_guide.md` plus an ADR-index update (see `post_4_9a_cleanup.md` for the durable task tracking). While the Phase 5 guide is open, also refresh its "Phase 4.9 prerequisite" paragraph to reflect the actual 4.9a + 4.9b + 4.9c shape that shipped (currently it describes only the rebalance-review half).
- **Mandatory pre-launch removal:** the `LLM_CLI_PATH`-with-consumer-OAuth path that Phase 3b shipped (and that the solo project owner uses for personal use) **must be removed entirely** before any second user signs up. The solo personal-use exception in ADR-0016 explicitly does not extend to multi-tenant deployment — what's gray-area-tolerated for one user becomes unambiguous OAuth abuse for many. All users on `LLM_BACKEND=anthropic_api` in Phase 5; remove the `agent_sdk` option from the user-facing config entirely or restrict it to deployments authenticated via per-user `ANTHROPIC_API_KEY` only.
- **Mandatory pre-launch removal #2:** auto-trade in `LIVE` mode is a single-user product behaviour. Phase 5 must either remove the auto-trade `LIVE` option from the multi-tenant offering entirely, or carry it forward only after additional regulatory work (most likely it ships as suggest-only-multi-tenant in v1 of the productized version, with auto-trade reserved for the solo deployment).
- **Mandatory pre-launch removal #3:** the **CNN Fear & Greed scrape** (ADR-0030, post-2026-06-18 renumber) is single-user-only tolerated grey area — `production.dataviz.cnn.io/index/fearandgreed/graphdata` is an undocumented endpoint accessed with browser-shaped headers to defeat anti-bot detection. CNN's Terms of Service almost certainly prohibit this; what's grey-area-tolerated for solo Jane becomes unambiguous abuse at multi-tenant scale (structurally identical to ADR-0016's OAuth CLI decision). Before any second user signs up in Phase 5, replace with one of: (a) a paid VIX/F&G feed; (b) graceful absence of the Market Sentiment widget in Phase 5 emails (preserves ADR-0022's degradation contract); (c) user-supplied API key configuration (BYO). See ADR-0030 for the full decision matrix and operational fragility contract.
- Deliverable: a second user can sign up, connect their Alpaca or Moomoo, define targets via the dashboard, and get their own weekly emails. Funds-added detection and quarterly/annual review prompts work for each user's account independently.

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

---

## Architecture Decision Records

ADRs in this workspace are collected inline here, alongside the phase entries that motivated them, rather than living as separate files. CLAUDE.md describes `docs/adr/0001-*.md` as the long-term **code-repo** convention; the planning workspace keeps the decision narrative in `product_plan.md`. When the code ships in the production repo, ADRs are split into individual files at that point.

**Live ADR sequence (as of 2026-06-18, post-renumber):**

- ADRs **0001–0023** — pre-Phase 4.9, referenced throughout this document and the phase guides; canonical text lives in the code repo's `docs/adr/` directory.
- ADR-**0024** — shipped with Phase 4.9a (multi-broker single-user data model). See the Phase 4.9a entry above; canonical text in the code repo.
- ADR-**0025** — Allocation donut chart as inline CID PNG (Pillow-rendered). Written in the code repo 2026-06-17; mirrored inline below.
- ADR-**0026** — SQLite DELETE journal mode + named-volume DB (WAL data-loss fix). Written in the code repo 2026-06-18; mirrored inline below.
- ADRs **0027–0032** — post-4.9a hardening batch from 2026-06-03 → 06-09 (Tavily-as-movers-fallback, movers tiers direction-aware, bars split-adjusted, CNN sentiment fragility + Phase 5 pre-launch removal #3, shared email components, suggestion `cancelled` terminal). **Renumbered 2026-06-18** from the original 0025–0030 reservation, after the code-repo agent independently took 0025 and 0026 for the donut and WAL ADRs. Full text below.
- ADR-**0033** — Snapshot one-`ts`-per-batch contract (broker-adapter invariant; every row in a sync batch shares one `ts`, enforced by `take_snapshot`). **Written** 2026-06-23 in `docs/adr/0033-snapshot-one-ts-per-batch.md` — must hold before 4.9c lands IBKR + Tiger. See post-4.9a §9.
- ADR-**0035** — Funds-flow detection via a cash-flow heuristic. **Written** 2026-06-30 (soak-window P2.3) in `docs/adr/0035-funds-detection.md`.
- ADRs **0034, 0036** — reserved for the Phase 4.9b household unit at resumption (household-target-as-optional; email-aggregation toggle).
- ADRs **0037–0038** — reserved for Phase 4.9c at resumption (IBKR adapter, Tiger adapter).
- ADRs **0039+** — reserved for Phase 5 and beyond (Phase 5 guide currently still allocates 0024–0031; will be renumbered when Phase 5 implementation begins, see "Mandatory pre-implementation cleanup" in the Phase 5 entry).

> **Renumbering note (2026-06-18).** Between 2026-06-09 (when I first wrote the §1–8 ADRs inline in this file at 0025–0030) and 2026-06-18, the code-repo agent independently authored two new ADRs as `docs/adr/0025-*.md` (donut chart) and `docs/adr/0026-*.md` (SQLite WAL fix). The code-repo numbering is authoritative; this section was renumbered to match. Anywhere a phase guide still references the older numbers, see the cross-reference tables in `post_4_9a_cleanup.md` for the mapping.

---

### ADR-0025 — Allocation Donut Chart as Inline CID PNG (Pillow-Rendered)

**Status:** Accepted. **Date:** 2026-06-17. **Commit:** `83515a8`.

**Context.** The daily email's Allocation section was text-only — a table of tickers, target%, current%, drift. Visually flat and hard to scan. The natural enhancement is a donut chart anchoring the section. Two non-trivial constraints made the "obvious" implementations infeasible: (1) **email clients strip `<svg>` tags** — Gmail, all Outlook variants, Apple Mail. SVG-based charts render as nothing. (2) **Email clients ignore CSS `conic-gradient`** — the cute "CSS-only donut" that works in browsers doesn't render in email. So the chart must be a raster image. Two delivery options remained: an external CDN URL (chart-as-a-service like QuickChart.io) or an inline embedded image. External CDN means a third-party HTTP roundtrip per email render plus a per-render privacy footprint; inline embedded means heavier email payload but zero external dependencies.

**Decision.** Render the donut server-side with **Pillow** (mature Python image library); embed in the email as an **inline Content-ID image via `multipart/related`** (renders in Gmail without "load remote images"; works offline). Implementation: `services/charts.py` (new) — `build_allocation_pie()` with 4× supersampling + LANCZOS downscale for clean anti-aliased edges; `ALLOC_PALETTE` shared between the donut and the HTML legend so colours don't drift. `services/email.py` — `SMTPEmailer` builds `multipart/related` when `inline_images` are given; `EmailSender` / `FakeEmailer` gain the optional `inline_images` param. Plain emails unchanged. `services/daily_report.py` — `AllocationSlice` + `_build_allocation_slices` (equity = positions + cash; top-8 then "Other"; cash last). `jobs/daily_report.py` — renders the PNG and attaches it; **render failure → legend-only, no broken image**.

**Rejected alternatives.** QuickChart.io / external chart service (HTTP roundtrip + third-party dependency + per-render data leakage — net negative); hand-built `<rect>` SVG (ignored by email clients); ASCII / Unicode block-character "chart" (works in plain-text; the marquee format is HTML); pre-baked image per portfolio shape (unfeasible at MT scale; freshness problem).

**Consequences.** Daily Allocation section gains a visual anchor that renders correctly across Gmail / Outlook / Apple Mail without "load remote images". Render failure gracefully degrades to legend-only. New runtime dep (`pillow>=10.4`); ~30MB Docker image growth. The `multipart/related` plumbing is general-purpose — any future inline-image email feature reuses the same `inline_images` parameter; sets the pattern going forward.

**References.** `services/charts.py`, `services/email.py::SMTPEmailer`, `services/daily_report.py::AllocationSlice`. Tests: `test_charts.py`, `test_email.py` (MIME structure), `test_daily_report_allocation.py`, `test_email_templates.py`.

---

### ADR-0026 — SQLite DELETE Journal Mode + Named-Volume DB (WAL Data-Loss Fix)

**Status:** Accepted. **Date:** 2026-06-18. **Commits:** `02b1859`, `d5b8d99`. **Severity: critical (silent data loss).**

**Context — silent data loss observed.** The §10 target reload for account 62 (adding TSLA, fixing the QQQ band) reported `updated` and verified the new rows present in the live DB. **Days later** the DB had silently reverted: TSLA was gone from `target_allocation` with no trace (the June-1 rows that should have been closed-and-superseded were never closed), while `positions_snapshot` rows kept advancing normally. TSLA correctly surfaced as *untracked* on the daily email — because as far as the live DB was concerned, it was genuinely no longer a target.

**Root cause.** The OLTP DB was running in **WAL journal mode** (set historically by the langgraph `SqliteSaver` in Phase 3b; the migration to `MemorySaver` reverted the *code path* but did not unwind the *engine-level pragma* — CLAUDE.md gotcha #12 was incomplete on this point) on a **macOS Docker Desktop bind mount** (`./data` ↔ container's `/app/data`). SQLite's own docs state explicitly that WAL "does not work on a network filesystem"; Docker Desktop's bind-mount virtualisation on macOS exhibits exactly that class of inconsistency. The symptom: committed transactions accumulated in `-wal` (the main `.db` lagged by ~15 hours; the host vs container even reported different sizes for the same file), and the un-checkpointed tail was dropped on a restart, reverting state to the last successful checkpoint.

How long had this been silently happening? Since Phase 3b — *potentially months*. We don't have a clean number for how many writes were lost; a retroactive consistency audit is warranted (separately tracked in `post_4_9a_cleanup.md`).

**Decision (two layers).** **Application layer — force the journal mode on every connection.** `db.py` registers a `connect` event listener that runs `PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;` on every new SQLAlchemy connection. The app is single-writer (CLAUDE.md convention #8); WAL bought nothing — its only contribution was the data-loss risk. `init_db` **fails fast** if the journal mode is still WAL after the pragma set. **Infrastructure layer — DB on a named volume, not a bind mount.** `docker-compose.yml`: OLTP DB moved off the `./data` bind mount onto a **named Docker volume** (`dbdata:/app/db`, `SQLITE_PATH=/app/db/investor.db`). Named volumes don't go through Docker Desktop's bind-mount virtualisation; SQLite + POSIX locking + mmap work correctly. Parquet bars and DuckDB analytics stay on `./data` (read-mostly, batch-written; not exposed to the locking bug). `Dockerfile`: `mkdir -p /app/db` as `appuser` so a fresh empty volume is writable on first boot (else "attempt to write a readonly database" crash loop on init). **One-time migration:** checkpointed the live WAL DB off-WAL, copied into the named volume (chown 1000), cutover. Verified: `PRAGMA journal_mode → delete`, `PRAGMA synchronous → FULL`, 13 targets including the previously-lost TSLA, 905 position rows intact, write path re-confirmed (snapshot count advanced 905 → 919 after a fresh sync). Old bind-mount DB retired (backup `investor.db.bak-pre-vol-2026-06-18`).

**Operational implications.** The live DB is **no longer at `./data/investor.db` on the host filesystem.** It lives inside the `me_invest_dbdata` Docker volume. Inspect: `docker compose exec app sqlite3 /app/db/investor.db ...`. Back up: `docker run --rm -v me_invest_dbdata:/db -v "$PWD":/out alpine cp /db/investor.db /out/`. **A careless `docker volume prune` wipes the DB.** Host-side backup tools (Time Machine on the previous `./data/` path) no longer cover it. The runbook task in `post_4_9a_cleanup.md` is now high-priority — a documented + verified backup-and-restore procedure must exist before this is considered fully resolved.

**Open follow-ups.** (1) **Retroactive data-integrity audit.** Pick a sample of writes you remember making across the soak period (target edits, mode promotions, suggestion accepts, broker-account onboards) and verify the DB matches your memory. If anything looks off, the silent-loss period extended further than just the TSLA case. (2) **Pragma audit on any future SQLite-touching library swap.** CLAUDE.md gotcha #12 needs to be expanded: *"if you ever swap a sqlite-touching library, audit the pragmas it set and unwind them explicitly; don't assume a code-level swap reverts engine-level state"*. The fail-fast WAL check on init prevents this specific recurrence, but the broader lesson applies to `synchronous`, `cache_size`, `temp_store`, `foreign_keys`, etc. (3) **Phase 5 implication.** The named-volume migration is correct for solo Jane on macOS Docker Desktop, but Phase 5 multi-tenant on Linux Docker would not have hit this specific bug (native bind mounts work). The fail-fast pragma check is still the right defence regardless. The deeper lesson — any deployment-substrate change deserves a verification pattern — belongs in the Phase 5 guide's pre-launch checklist.

**Consequences.** Silent-loss failure mode structurally closed; `init_db` fails fast if the pragma drifts; named-volume move removes Docker Desktop's bind-mount virtualisation from the locking path. Cost: DB is less visible to host backup tooling; one-time runbook + restore-verification work is now urgent. The retroactive-audit recommendation is the only thing that bounds the unknown — without it, "how much data was lost between Phase 3b and 2026-06-18" stays open-ended.

**References.** `db.py` (`connect` event listener, init_db fail-fast check), `docker-compose.yml` (named volume), `Dockerfile` (mkdir /app/db). Test: `test_db.py::test_pragmas_force_delete_journal_not_wal`. CLAUDE.md gotcha #12 (incomplete on this point; needs expansion). Related: `post_4_9a_cleanup.md` (runbook task elevated to high-priority; retroactive audit added).

---

### ADR-0027 — Tavily as Third-Fallback in Movers News Pipeline

**Status:** Accepted (supersedes `phase_4_5_guide.md` §8 deferral).
**Date:** 2026-06-04. **Commit:** `c05d581`. **Extends:** ADR-0020 (Tavily as weekly-context provider).

**Context.** `phase_4_5_guide.md` §8 explicitly deferred *"Tavily-as-third-fallback in Phase 3b"* citing three reasons: (1) Tavily's hours-to-day general-web crawl cadence vs Alpaca News (Benzinga, minutes-fresh); (2) Alpaca/Finnhub tagged-at-source signal-to-noise vs Tavily's general web (blog posts, opinion, listicles); (3) different use cases — daily movers ask "what specific same-day company news moved this ticker?" vs Tavily's weekly macro/sector synthesis. The deferred condition — *"if 'no news explanation' annotations turn out to be common"* — turned out true during the 4.9a soak for thinly-covered tickers (notably crypto-related ETFs and small-cap names).

**Decision.** When Alpaca News + Finnhub combined return **fewer than 3 articles** for a mover, the movers job invokes Tavily as a third fallback via `fetch_tavily_news` in `services/news.py`. Tavily results are **display-only** in the movers email and **never persisted to `news_event`** (preserves ADR-0020's informational-not-advisory boundary; no path to the suggestion engine, the LLM classifier inputs, or any persistent state). Tavily-sourced articles are visually labelled (`source="tavily"`) so the reader knows the lower-confidence provenance. Mondays use a 48h lookback. Tavily monthly query cap applies — operationally ~25 calls/month additional, well within the 1000-call free-tier ceiling.

**Consequences.** "No news explanation" annotation rate drops materially on thinly-covered tickers. ADR-0020's architectural separation holds — Tavily content remains informational. The Tavily hours-to-day crawl lag remains real for major same-day events; the engine surfaces what Tavily has at query time and trusts the reader to weigh provenance. Phase 4.5 §8's "deliberately NOT used in movers" guidance is superseded — CLAUDE.md gotcha to be updated.

**References.** Supersedes `phase_4_5_guide.md` §8. Extends ADR-0020. Operational sibling: `services/news.py::fetch_tavily_news`, `_CRYPTO_NEWS_SYMBOLS`, `_alpaca_news_symbol`. Related correctness fix in same commit: `BTC` → `BTC/USD` Alpaca symbol-format fix.

---

### ADR-0028 — Movers Tiers are Direction-Aware and Reset per ISO Week

**Status:** Accepted. **Date:** 2026-06-09. **Commit:** `1c38fd6`.

**Context.** The movers job implements a tiered-threshold alert system to suppress same-direction noise within a measurement period (a ticker that fires the 5% alert shouldn't keep re-alerting on every 0.1% intra-day move). The **MU whipsaw case** surfaced two structural bugs: MU fired a −10.9% alert one week, bounced to +9.7% the next, and stayed silent. *Bug 1 — direction-blind latching:* tier was tracked on `abs(pct)`, so a sign-flip from −10% to +9% was treated as same-tier continuation and suppressed (anti-spam machinery silenced exactly the cases — direction reversals — the user most wants to see). *Bug 2 — cross-week state bleed:* tier state persisted across weeks even though the metric is *today vs prior-Friday close* (baseline rolls weekly).

**Decision.** Tier state now carries both **direction** (derived from the signed `last_pct_change` already stored — no migration) and **measurement week** (via ISO week, ET, of `last_triggered_at`). A sign flip starts a fresh tier in the new direction; same-direction moves still escalate tier-by-tier (anti-spam preserved). When the ISO measurement week changes, tier state resets — next week starts a fresh ladder against the new Friday baseline.

**Consequences.** Direction reversals re-alert as they should; MU-whipsaw failure mode closed. Anti-spam preserved within a week. No schema change. Brief flurry of alerts in the first ISO week after deployment (transient). ISO-week boundary aligns with the prior-Friday-close baseline convention by accident — document the alignment so a future timezone change doesn't break it. Holiday-shortened weeks (Thanksgiving, Christmas Eve) use the same ISO boundary; manually verify behaviour the first time this hits.

**References.** `jobs/movers.py::_should_alert`, `_iso_week`. Class of bug related to Phase 4.8's "naive 7-day last-week math" — different module, same lesson (state that should reset on a calendar boundary; signed quantities collapsed to magnitudes that hide direction flips).

---

### ADR-0029 — Bars Stored Split-Adjusted (`Adjustment.SPLIT`); SR-Level Re-Backfill Procedure

**Status:** Accepted — **data-semantics-changing**. Read carefully before reading any pre-`85053ca` bars or `sr_level` rows.
**Date:** 2026-06-08. **Commit:** `85053ca`.

**Context.** The weekly review for 2026-06-08 surfaced a nonsensical suggestion: ticker `BTC` showed nearest support as `swing_low_5bar $5.96` while trading at ~$27. Three layers: (1) the ticker `BTC` is the **Grayscale Bitcoin Mini Trust ETF**, not literal Bitcoin (user mental model and engine model had silently misaligned); (2) `BTC` did a **5:1 reverse split in November 2024**; (3) `services/bars.py::update_bars` was fetching **RAW (unadjusted)** bars (Alpaca's default), so pre-split 2024 history at $4–6 sat in the same Parquet file as post-split $25–30 current range. The fractal-low detector flagged the pre-split $5.96 as a swing low. Verified vs Alpaca: RAW $5.62 → SPLIT-adjusted $28.10 (exactly 5.0×).

**Decision.** Bars are stored **split-adjusted**. `services/bars.py::update_bars` passes `adjustment=Adjustment.SPLIT` to `StockBarsRequest`. **Splits only, not dividends** — splits reflect prices the market actually traded at after the corporate action; dividend-adjusted prices represent the holder's total-return, not market-traded prices. `services/levels.py::build_nearby_levels` gains a defence-in-depth `max_distance_pct` parameter (default 0.50): levels >50% from current price are dropped, regardless of how they were computed.

**Re-backfill procedure (one-time, manual; executed 2026-06-08).** Stop app → back up Parquet dir → delete `data/bars/*.parquet` → restart app → `POST /admin/reload-targets` (full re-fetch) → `POST /admin/run-weekly-suggestions` (recompute `sr_level`) → spot-check a historically-split ticker. Verified: BTC min low $4.4 → $22.1 (5.0×), nearest support now ~$27.80 (≈ 1% from current).

**Consequences.** S/R levels for historically-split tickers no longer surface phantom regimes. Defence-in-depth: 50%-distance filter survives any future regression in bar-adjustment handling. Future ticker splits handled automatically. ⚠ **The pre/post-`85053ca` data-semantics boundary is silent**: anyone restoring from a pre-06-08 snapshot must re-backfill bars or the inconsistency reappears. Dividends deliberately not adjusted — for high-dividend ETFs (SCHD ~3.5%/yr, JEPI ~7%/yr), cumulative effect over multi-year backfills is mildly stale; tracked as a follow-up. The 50%-distance filter may mask genuine signal in deep downtrends (a ticker with its only support 60% below current now silently skips).

**References.** `services/bars.py`, `services/levels.py::build_nearby_levels`. Follow-up tracked in `post_4_9a_cleanup.md`: ticker-name annotation in emails (so "BTC = Grayscale Bitcoin Mini Trust ETF" surfaces to prevent the cognitive mismatch earlier).

---

### ADR-0030 — CNN Sentiment Endpoint with Browser-Shaped Headers; Phase 5 Pre-Launch Removal

**Status:** Accepted (extends ADR-0022); **flagged as Phase 5 mandatory pre-launch removal #3**.
**Date:** 2026-06-07. **Commit:** `acd9a5c`. **Extends:** ADR-0022 (`SentimentClient` Protocol).

**Context.** ADR-0022 introduced `SentimentClient` Protocol with `FinnhubCNNSentimentClient` and a "never raise on failure" contract. Between Phase 4.7 ship and 2026-06-07, the implementation **never populated non-NULL values** — every `weekly_market_context` row landed with NULL `vix` and NULL `fear_greed_score`. Two pre-existing structural causes: (1) **Finnhub free tier does not serve `^VIX`** (`quote("^VIX")` returns `c=0`; premium tier serves it, free tier silently does not); (2) **CNN's `production.dataviz.cnn.io/index/fearandgreed/graphdata` endpoint returns HTTP 418 ("I'm a teapot") to bot User-Agents** — undocumented, not part of public API surface, active anti-bot detection by User-Agent string. Side-effect: the CNN `graphdata` JSON also carries the latest VIX under `market_volatility_vix`; one CNN call services both.

**Decision.** `_fetch_cnn` sends browser-shaped headers (`User-Agent` recent-Chrome string, `Accept`, `Origin`/`Referer` → cnn.com) — bypasses 418 in normal operation; returns `(score, label, vix)`. Finnhub kept only as a VIX fallback (invoked if CNN fails and a Finnhub premium key is configured). ADR-0022's "never raise on failure" contract preserved: any fetch failure → WARNING log + None return → Market Sentiment widget silently hides.

**Operational fragility contract.** The CNN endpoint is undocumented, unsupported, subject to anti-bot countermeasures at any time. Expected response matrix: (a) header tweak when CNN updates anti-bot; (b) accept silent NULLs until a paid feed is wired; (c) escalate to paid feed. Watch for `_fetch_cnn` WARNINGs in logs.

**Phase 5 Mandatory Pre-Launch Removal #3.** The CNN scrape is **single-user-only tolerated grey area**, structurally identical to ADR-0016's `LLM_CLI_PATH` consumer-OAuth decision. CNN's ToS almost certainly prohibits automated scraping; grey-area-tolerated for solo Jane → unambiguous abuse at multi-tenant scale. Before any second user signs up in Phase 5, replace with: (1) a paid feed (Polygon, Tradier, Finnhub Premium for VIX; F&G is harder to license cleanly — consider Alternative.me's crypto-focused F&G as non-equivalent, or derived in-house from put/call + breadth + momentum); (2) graceful absence of the Market Sentiment widget for Phase 5 users (preserves ADR-0022's degradation contract; lowest-effort path); (3) user-supplied API key (BYO; higher onboarding friction).

**Consequences.** VIX and F&G populate; Market Sentiment widget renders. Fragility flagged operationally. Phase 5 pre-launch budget gains an item with either real licensing cost or UX downgrade. The `User-Agent` string is now load-bearing config; periodic refresh recommended (annual).

**References.** Extends ADR-0022 with implementation specifics. Phase 5 pre-launch removal alongside ADR-0016 and existing #2 (auto-trade LIVE multi-tenant decision). `services/sentiment.py::_fetch_cnn`.

---

### ADR-0031 — Shared Email Components and the Jinja Autoescape Trap

**Status:** Accepted. **Date:** 2026-06-07. **Commit:** `1aa36cd`.

**Context.** Through Phases 4.5, 4.7, 4.8 the four email templates (daily report, weekly suggestions, weekly review, movers) grew organically with their own markup, colour choices, typography. By 06-07 they had visibly drifted: levels-table gating differed, untracked-positions banners had different shades of red, accessibility was inconsistent. A specific bug uncovered the structural problem: HTML entities (`&mdash;`, `&hellip;`) inside Jinja `{{ }}` expressions silently autoescape to literal `&amp;mdash;` rendering as `&mdash;` in the email body — per-occurrence fix is trivial but any future template author can re-introduce it.

**Decision.** Two shared component files. `templates/_components.html.j2` — one token palette (WCAG-AA on white), one type scale, macros (`header / footer / preheader / section / subsection / untracked_box / levels_table / responsive_style`). `templates/_sentiment.html.j2` — redesigned Market Sentiment widget (two metric cards, navy numerals, value-derived semantic colour, 5-band F&G strip, single-card fallback). All four email templates MUST import from `_components.html.j2`. Any HTML entity inside Jinja `{{ }}` must be replaced with the actual Unicode character (`—`, `…`, `©`). Preview workflow: render to HTML *and* PNG via Chrome headless, get visual approval, then deploy.

**Consequences.** Emails visually consistent and accessibility-correct. Mobile rendering correct across the four. SMA200 ETF-only gate enforced via shared `levels_table` macro (kills per-template drift). Future email changes have higher coordination cost — touching a shared macro affects all four; worth it (previous independence was buying drift, not flexibility). Outlook (desktop Windows) is the email client most likely to silently break new CSS additions; verify after any change to `_components.html.j2`.

**References.** `templates/_components.html.j2`, `templates/_sentiment.html.j2`. Tests: `tests/test_email_templates.py` (autoescape-leak guard, ETF-only MA200, sentiment colour bands), `tests/test_email_indicators.py`.

---

### ADR-0032 — Suggestion Status `cancelled` is Terminal; Auto-Trade Ignores It

**Status:** Accepted. **Date:** 2026-06-09. **Commits:** `72d0d5c` … `0b48307`.

**Context.** Through Phase 4 the order_suggestion lifecycle had `pending | accepted | rejected | expired`. The un-accept product question — "I changed my mind after accepting" — had no first-class representation. Two operational gaps: (1) no way to un-accept without going to the broker UI; the post-Phase-4 GTC update excluded `broker_cancelled` execution rows from `_check_idempotency` so legitimate cancel-and-re-place flows worked, but as a side-effect a manual broker-UI cancel + still-`accepted` suggestion meant auto-trade would re-place the order the next morning; (2) no audit distinction between "declined before acting" and "changed mind after acting".

**Decision.** New terminal status **`cancelled`**: `pending | accepted | rejected | expired | cancelled` (string column; no migration). Distinct semantics: `rejected` = declined *before* acting (no `order_execution` ever existed); `cancelled` = un-accepted *after* acting (an `order_execution` may have been routed); `expired` = no action by Friday rollover. `auto_trade._fetch_accepted_unexecuted` selects only `accepted` rows → `cancelled` is ignored by re-placement loop, closing the GTC-update footgun.

**Shared cancel helper** `services/orders.py::cancel_working_execution`: re-queries broker for authoritative status, then: filled → refuse; partially_filled → cancel remainder (filled shares stand); working → cancel + flip to `broker_cancelled`; terminal/cancel-failure → leave for reconciliation. The expiry sweep was refactored onto the same helper (gained partial-fill handling). **Un-accept entry point** `services/unaccept.py::unaccept_suggestion`: guard `accepted`, cancel via helper, refuse if fully filled, else flip suggestion to `cancelled`. **Endpoints**: GET `/suggestions/{sid}/unaccept` renders confirm page (no side effect; shows live broker status); POST performs the action; HMAC-signed via `sign_action(sid, "unaccept", …)` for email-linkable use. Admin variant: `POST /admin/suggestions/{sid}/unaccept`. **Daily email "Open & Committed Orders"** section lists this-week `accepted` suggestions with latest execution status + signed Un-accept link on cancellable rows.

**Known gap — manual broker-UI cancel without un-accept link click.** A user who cancels in the broker UI and *doesn't* click the un-accept link leaves the suggestion `accepted`; reconciliation marks execution `broker_cancelled`; auto-trade re-places next morning (exactly the footgun this ADR set out to close). The un-accept path closes the footgun *only when the user uses the link*. Mitigation deferred (tracked in `post_4_9a_cleanup.md`): detect manual-broker-cancel via reconciliation and auto-mark `cancelled` if no user action within N hours.

**Consequences.** First-class un-accept path. Audit distinction between "declined" and "changed mind" preserved. Shared cancel helper deduplicates a previously-divergent code path. Negative: GET-confirm queries broker for live status — URL prefetchers (Microsoft 365 SafeLinks, Slack unfurl, Gmail link-preview) hit the GET on link-hover; benign at solo scale; could rate-limit broker at MT scale (tracked).

**References.** `services/orders.py::cancel_working_execution`, `services/unaccept.py::unaccept_suggestion`, `jobs/suggestion_expiry.py` (refactored onto the helper), `services/daily_report.py::CommittedOrderRow`. Templates: `daily_report.*`, `unaccept_confirm.html.j2`, `unaccept_result.html.j2`. Related: post-Phase-4 GTC update introduced the `broker_cancelled` re-place behaviour this ADR partially closes.
