# Phase 1 Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 1 — Portfolio Email & Bar Backfill  
**Code complete:** 2026-05-04  
**Pre-tag cleanup:** 2026-05-05  
**Git tag:** `v0.1.0-phase-1` — deferred until 5 consecutive trading-day emails received (earliest 2026-05-09 per phase_1_guide §11 row 14)

---

## 1. Scope vs. delivery

The product plan defined Phase 1 as:

> Daily recurring sync + portfolio snapshot email (positions table, gap table, drift alerts). 2-year OHLCV bar backfill stored as Parquet. Definition of done: email lands in inbox on 5 consecutive trading days without manual intervention.

All planned deliverables were met. Two bugs were found and fixed during deployment (templates path missing in Docker image; detached SQLAlchemy session after context manager exit).

---

## 2. What was built

### New endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/drift` | GET | Gap rows where `band_status != "in_band"` only |
| `/admin/run-daily-report` | POST | Manual trigger for the full daily report job |

### Updated endpoints

| Endpoint | Change |
|---|---|
| `/gap` | Added `band_status` field (`"under"` / `"in_band"` / `"over"`) to every row |

### Scheduler

`DateTrigger` (one-shot, 30s after startup) replaced with `CronTrigger`:

```
Mon–Fri at 16:15 America/New_York
misfire_grace_time = 1800s (runs within 30 min if box was offline at fire time)
```

### Daily report job

Order of operations on each firing:

1. `take_snapshot()` — pulls live positions from Alpaca, persists to `positions_snapshot` and `broker_account`
2. `compose_daily_report()` — reads DB, returns `DailyReport` (pure function, no I/O)
3. `render_template()` — renders HTML and plain-text via Jinja2
4. `emailer.send()` — SMTP via Gmail App Password (STARTTLS on port 587)

Email subject: `Portfolio — YYYY-MM-DD (equity $XX,XXX)`

### Email template sections

| Section | Content |
|---|---|
| Header | Date, equity, cash, broker/mode |
| Drift alerts | Yellow banner — only shown when tickers are outside their rebalance band |
| Allocation table | Ticker, qty, avg cost, market value, current %, target %, gap %, band status (✓ / ⚠ under / ⚠ over) |
| Gap summary | Top 3 underweight (buy) + top 3 overweight (trim) |
| Footer | "No orders are placed automatically." |

Both HTML (inline styles, no external images) and plain-text versions are rendered and sent as a MIME multipart email.

### Bar backfill scripts

| Script | Purpose |
|---|---|
| `scripts/backfill_bars.py` | One-shot: fetch 2 years of OHLCV from Alpaca IEX feed, write one Parquet per ticker to `data/bars/<TICKER>.parquet` |
| `scripts/update_bars.py` | Daily: append yesterday+today bars to each existing Parquet, deduplicating by timestamp |

Run: `uv run python scripts/backfill_bars.py`

Result: `data/bars/VOO.parquet`, `data/bars/QQQ.parquet`, etc. (~500 rows × 6 files ≈ 250 KB).

---

## 3. New service layer

| File | Role |
|---|---|
| `src/investor/services/email.py` | `EmailSender` Protocol, `SMTPEmailer` (real), `FakeEmailer` (tests) |
| `src/investor/services/render.py` | `render_template(name, **ctx)` — Jinja2 `FileSystemLoader` pointed at `templates/` |
| `src/investor/services/daily_report.py` | `AccountSnapshot` dataclass + `DailyReport` dataclass + `compose_daily_report(session)` |
| `src/investor/jobs/daily_report.py` | `run_daily_report(settings, adapter, emailer)` — orchestration, re-raises on failure |

### `AccountSnapshot` (new in Phase 1)

SQLAlchemy ORM objects become "detached" (unusable) after their session closes. `AccountSnapshot` is a plain frozen dataclass that copies `broker`, `mode`, `cash_usd`, `equity_usd` out of the ORM object while the session is still open. Templates only ever see plain Python values.

```python
@dataclass(frozen=True)
class AccountSnapshot:
    broker: str
    mode: str
    cash_usd: float
    equity_usd: float
```

---

## 4. Architecture decisions made in Phase 1

### ADR-0002 — Three-Tier Storage Architecture

Rewrote the foundational storage ADR (previously titled "Schema Migrations with Alembic + DuckDB", which was stale). The new content documents the permanent three-tier split:

| Tier | Technology | Purpose |
|---|---|---|
| OLTP | SQLite via SQLAlchemy + Alembic | Transactional tables: targets, positions, account, suggestions |
| Analytics | DuckDB (direct Python, not via SQLAlchemy) | Vectorized queries over Parquet bars |
| Cold storage | Parquet under `data/bars/` | Survives DB migrations; queryable via DuckDB `read_parquet()` |

DuckDB's SQLite scanner allows cross-tier joins in a single query: `ATTACH 'investor.db' AS sq (TYPE SQLITE)`. Phase 5 path: SQLite → Postgres for OLTP; DuckDB/MotherDuck stays for analytics.

### ADR-0003 — Schema Migrations with Alembic + SQLite

Rewrote the migrations ADR (previously titled "SQLite OLTP + DuckDB Analytics", which duplicated ADR-0002's content). New content covers:
- `SQLiteImpl` is built-in to Alembic — no stub required (contrast with old stale DuckDB-era content)
- `render_as_batch=True` in `migrations/env.py` — SQLite cannot `ALTER COLUMN`/`DROP COLUMN` directly; batch mode recreates the table transparently
- Baseline strategy: `Base.metadata.create_all(checkfirst=True)` + `alembic upgrade head` on startup; coexist because `create_all` is idempotent and the baseline revision is a no-op
- Revision history table via `meta` table added in revision `71b1bd302b7e`

### ADR-0004 — Parquet files + DuckDB `read_parquet()` for bars

Bar data lives in `data/bars/<TICKER>.parquet`. Queried via `import duckdb` directly:

```python
conn = duckdb.connect()
conn.execute("SELECT * FROM read_parquet('data/bars/*.parquet')")
```

Why not a SQLite table: SQLite is row-oriented; analytical queries (moving averages, cross-ticker scans) need columnar vectorized execution that DuckDB provides natively. Parquet files also survive DB schema migrations unchanged.

### ADR-0005 — Re-raise on email failure

`run_daily_report` does not catch exceptions from `emailer.send()`. If SMTP fails, APScheduler logs the error and the `misfire_grace_time` drives a retry on next startup. Silent swallowing would make the monitoring signal (did the email arrive?) meaningless.

---

## 5. `app.state` wiring (Phase 1 change to `main.py`)

On startup, `lifespan()` now builds the adapter and emailer once and stores them on `app.state`:

```python
app.state.settings = _settings
app.state.adapter  = make_adapter(_settings)
app.state.emailer  = SMTPEmailer(...)
```

The scheduled job and the manual endpoint both retrieve from `app.state`, ensuring they share the same live objects.

---

## 6. Bugs found and fixed during deployment

### Bug 1 — `templates/` not copied into Docker image

**Symptom:** `POST /admin/run-daily-report` → `'daily_report.html.j2' not found in search path: '/app/templates'`  
**Root cause:** `Dockerfile` had no `COPY templates/ ./templates/` line.  
**Fix:** Added `COPY templates/ ./templates/` after `COPY config/ ./config/`.

### Bug 2 — Detached SQLAlchemy instance after `session_scope()` exit

**Symptom:** `Instance <BrokerAccount> is not bound to a Session; attribute refresh operation cannot proceed`  
**Root cause:** `compose_daily_report()` returned the ORM `BrokerAccount` object inside `DailyReport`. The session closed when the `with session_scope()` block exited. When the Jinja2 template later accessed `report.account.equity_usd`, SQLAlchemy attempted a lazy reload but had no open connection.  
**Fix:** Introduced `AccountSnapshot` — a plain frozen dataclass populated inside the session before it closes.  
**Regression test added (2026-05-05):** `test_account_snapshot_survives_session_close` in `tests/test_daily_report.py` — closes the session after `compose_daily_report()` and asserts fields are still readable. Catches any future regression where an ORM object is accidentally returned instead of the dataclass.

---

## 7. Test coverage

| Test file | Tests | Coverage |
|---|---|---|
| `tests/test_config.py` | 8 | Settings + YAML loader (unchanged from Phase 0) |
| `tests/test_gap.py` | 10 | All Phase 0 gap tests + 2 new `band_status` tests (under / over) |
| `tests/test_load_targets.py` | 5 | Hash-based target dedup (unchanged from Phase 0) |
| `tests/test_email.py` | 3 | `FakeEmailer` records, `SMTPEmailer` raises on empty credentials |
| `tests/test_daily_report.py` | 3 | Empty DB report, drift alerts populated correctly, session-close regression (added 2026-05-05) |
| `tests/test_integration_alpaca.py` | 1 | Full chain against live Alpaca paper account (skips without API keys) |

**Total: 29 unit tests** (+ 1 integration). All pass on `sqlite:///:memory:`.

---

## 8. Known issues and limitations

### Cash-buffer denominator — evaluated, no bug

Phase 1 uses `weight_pct = market_value / equity_usd * 100`. Targets are validated to sum to `100 - cash_buffer_pct` (= 95%). Both sides of the gap formula use total equity as the denominator — there is no systematic under-target bias when fully allocated. A comment was added to `gap_allocation.sql` to document this invariant and prevent a future "fix" that would introduce an actual bug:

```sql
-- weight_pct is stored as pct of total equity (incl. cash).
-- Targets sum to 100 - cash_buffer_pct, so both sides share the same denominator — no scaling needed.
```

### Bar data not yet wired into the scheduler

`scripts/update_bars.py` exists and works but is not called by the scheduler. Daily bar updates require manual runs until Phase 2 wires it into a job.

### No support/resistance levels or order suggestions

Phase 1 delivers raw OHLCV bars only. SMA, RSI, support/resistance computation, and the `order_suggestion` table are Phase 2 scope.

### No LLM news triage

Phase 1 has no Anthropic API calls. The `anthropic` package is already in `pyproject.toml` (added to deps in the product plan phase) but unused until Phase 3.

### `positions_snapshot` still grows unbounded

Every daily sync appends rows. At 1 sync/day × 6 tickers × 252 trading days = ~1,500 rows/year. Manageable at current scale; a pruning job should be considered by Phase 2 or 3.

### Email success is self-reported

There is no inbound check (e.g., "did the email actually arrive?"). The definition of done requires 5 consecutive received emails — verify manually in Gmail.

---

## 9. Environment and dependencies

- **Python:** 3.12 (`.python-version` pinned to `3.12` via `uv python pin 3.12` on 2026-05-05; CPython 3.12.13 installed on host — version drift from earlier 3.13 host default eliminated)
- **Key new runtime deps:** `pyarrow>=18.0` (Parquet I/O for pandas)
- **Key new dev usage:** `jinja2` (was already a transitive FastAPI dep, now used directly)
- **Docker base image:** `python:3.12-slim`
- **Templates dir:** `templates/` at project root; `COPY templates/ ./templates/` in Dockerfile

---

## 10. Recommended Phase 2 starting point

Based on the product plan and current state, Phase 2 should deliver:

1. **Technical indicators** — SMA-20, SMA-50, RSI-14 computed from Parquet bars via DuckDB; exposed on a new `/indicators` endpoint
2. **Support/resistance levels** — rolling pivot points or recent swing highs/lows per ticker
3. **Weekly order suggestions** — `order_suggestion` table, `status = "pending"`, surfaced via `/suggestions`
4. **Daily bar update wired into scheduler** — add `update_bars()` call inside `run_daily_report` or as a separate CronJob
5. **Indicators in the daily email** — extend `DailyReport` with indicator rows; update templates

### Files Phase 2 will primarily touch

| File | Why |
|---|---|
| `src/investor/services/indicators.py` | New — DuckDB queries over Parquet bars |
| `src/investor/services/levels.py` | New — support/resistance computation |
| `src/investor/services/suggest.py` | New — gap + level → order suggestion logic |
| `src/investor/models.py` | Add `OrderSuggestion` ORM model |
| `src/investor/jobs/daily_report.py` | Add bar update + indicator fetch before compose |
| `src/investor/services/daily_report.py` | Extend `DailyReport` with indicators and suggestions |
| `templates/daily_report.html.j2` | Add indicators table and suggestions section |
| `migrations/` | New Alembic revision for `order_suggestion` table |
| `scripts/update_bars.py` | Already written; just wire it in |

---

## 11. Pre-tag cleanup (2026-05-05)

Before tagging `v0.1.0-phase-1`, six issues were identified and resolved. Changes are committed to `main`.

| # | Issue | Resolution |
|---|---|---|
| 1 | ADR-0002 and ADR-0003 had swapped/stale content from the DuckDB-era OLTP experiment | Full rewrites in place: ADR-0002 → "Three-Tier Storage Architecture"; ADR-0003 → "Schema Migrations with Alembic + SQLite" |
| 2 | ADR-0004 cross-reference pointed to wrong ADR after the swap | One-line fix: `ADR-0003` → `ADR-0002` in `docs/adr/0004-bar-storage.md` |
| 3 | Cash-buffer denominator logic was not documented; future agent might add a "fix" that introduces a bug | Evaluated — no bug exists (both sides of gap formula use total equity). Added 2-line comment to `gap_allocation.sql` |
| 4 | Bug 2 (detached instance) had no regression test | Added `test_account_snapshot_survives_session_close` to `tests/test_daily_report.py` (total tests: 29) |
| 5 | `DailyReport.positions` annotation comment was ambiguous about session-safety | Comment updated to `# Raw SQL named-tuple rows (not ORM objects) — session-safe.` |
| 6 | `.python-version` was `3.13` (host default); `pyproject.toml` and Docker target `3.12` | Pinned to `3.12` via `uv python pin 3.12`; host now runs CPython 3.12.13 |
