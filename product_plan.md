# Long-Term Investor Assistant — Product Plan (v1)

**Owner:** Jane · **Date:** 2026-04-24 · **Stage:** Ideation

---

## 1. Product in one paragraph

A self-hosted, always-on assistant for a long-term investor. The user declares a watchlist and a target allocation. The system pulls positions and prices daily, computes the gap between current and target, identifies support/resistance levels on each ticker, and emails a weekly order suggestion plus daily news/price alerts (anything ±5 %, ±10 % vs. last week's close). The user places orders themselves — the product never touches the send-order button in v1. Designed for one user (Jane) first, with a clean path to becoming a multi-tenant product later.

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

### Phase 2 — Technical levels & weekly order suggestions (1.5–2 weeks) — current
- Resolve Phase 1 carryovers above (~1 evening): write ADRs 0002–0003, verify cash-buffer fix, add Bug 2 regression test, sweep `AccountSnapshot` pattern, pin Python 3.12 on host, add admin-token auth.
- Wire `update_bars.py` into the daily report job so bars stay fresh.
- Compute indicators from Parquet bars via DuckDB: SMA-20 / 50 / 200, EMA-21, RSI-14, MACD. Expose on `/indicators`.
- Compute support/resistance levels per ticker: classical pivots (daily/weekly/monthly), MA bands as dynamic S/R, swing highs/lows via fractal method. Persist to `sr_level` table.
- Build `order_suggestion` table + the suggestion engine: gap engine + nearest support → limit-buy suggestion sized to close half the gap; over-band tickers get trim suggestions at nearest resistance. Apply guards (cash sufficiency, min share, wash-sale stub).
- New weekly cron Sunday 18:00 ET: `run_weekly_suggestions` — update bars, compute indicators + levels, generate suggestions, email "Orders for the week of Mon DD."
- Add a compact "Levels at a glance" section to the daily email (current price, distance from 50/200-SMA, RSI-14, nearest S/R).
- Deliverable: a Sunday evening email titled "Orders for the week of Mon DD" with concrete suggestions you read and feel are sensible — not "what?" Phase 2 done = first credible weekly email lands.

### Phase 3 — Daily monitor + LLM news (1–2 weeks)
- EOD job: for every watchlist ticker, compute `pct_vs_last_week_close`.
- Triage buckets: |∆| ≥ 5 %, ≥ 10 %, ≥ 15 %.
- For every flagged ticker, pull news from Alpaca News (+ Finnhub fallback) for last 24 h.
- Pass the news batch to Claude with a prompt like:
  ```
  For each headline, classify: material / noise.
  If material, write a 1-sentence summary and a bullish/bearish/neutral label.
  Return JSON.
  ```
- De-dupe by headline hash. Store in `news_event`.
- Email: "Movers — 2026-04-24" with ticker cards: ∆, last week close, today, top 3 material headlines with LLM summary.
- Deliverable: one email per trading day EOD (around 16:30 ET) covering only flagged tickers. No mail if nothing moved — keep the signal-to-noise high.

### Phase 4 — Weekly review workflow (1 week)
- Friday EOD job: build the weekly review:
  - Realized PnL for the week.
  - Orders suggested vs. filled (did you place them?).
  - New gap vs. target after this week's moves.
  - Big events flagged by the daily monitor.
  - **Proposed orders for next week** (same engine as Phase 2, refreshed).
- Email with two buttons: "Accept all" / "Review each" — these are `mailto:` or signed URLs back to your FastAPI endpoints that mark suggestions as accepted/rejected. No orders are placed — just bookkeeping for post-mortems.
- Deliverable: Friday 5 PM ET weekly review, plus an audit trail of "what I suggested vs. what actually happened."

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
- Deliverable: a second user can sign up, connect their Alpaca or Moomoo, and get their own weekly emails.

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
