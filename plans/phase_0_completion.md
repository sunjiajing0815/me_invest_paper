# Phase 0 Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 0 — Foundation  
**Completed:** 2026-04-28  
**Git tag:** `v0.0.1-phase-0` (branch: `main`, HEAD: `207622a`)

---

## 1. Scope vs. delivery

The product plan defined Phase 0 as:

> Scaffold: Python project, FastAPI, SQLAlchemy, APScheduler, Dockerfile, compose file. BrokerAdapter interface + Alpaca implementation (read-only first). Deliverable: `docker compose up` runs, `/health` responds, `python scripts/sync_positions.py` writes a snapshot row.

All planned deliverables were met. Several unplanned improvements were also made during implementation (see §4).

---

## 2. What was built

### Services and endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Status, broker name, last sync timestamp, active target count |
| `/positions` | GET | Latest snapshot per ticker (most recent row per ticker via window function) |
| `/gap` | GET | Allocation gap vs. targets in % and USD, sorted by abs(gap) descending |
| `/admin/run-sync` | POST | Ad-hoc sync trigger; runs synchronously, returns when complete |

### Scheduled sync

On startup, APScheduler fires a one-off sync 30 seconds after launch (`DateTrigger`). Phase 1 will replace this with a recurring daily `CronTrigger`.

### CLI scripts

| Script | Purpose |
|---|---|
| `scripts/load_targets.py` | Seeds `target_allocation` from `config/targets.yaml`. Idempotent — skips write if targets are unchanged, prints a diff when something changes. |
| `scripts/sync_positions.py` | One-shot pull of positions from Alpaca; writes to `positions_snapshot` and `broker_account`. |

### Broker adapter

`AlpacaAdapter` wraps `alpaca-py` `TradingClient` in paper mode. All broker-specific types are converted to domain dataclasses (`Account`, `Position`) at the boundary. No code outside `src/investor/brokers/` imports the Alpaca SDK.

### SQL

All SQL lives in `src/investor/sql/*.sql` and is loaded at import time via `src/investor/queries.py`. No inline SQL anywhere in Python code.

---

## 3. Data model — current state

### `target_allocation`

Time-versioned. Load script uses close-and-insert pattern — never `UPDATE` in place. Current open row has `effective_to = NULL`.

```
id | ticker | target_pct | band_low_pct | band_high_pct | effective_from | effective_to
```

### `positions_snapshot`

Append-only. Every sync adds new rows. `/positions` and gap query select the latest row per ticker using `ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC)`.

```
id | account_id | ts | ticker | qty | avg_cost | market_value | weight_pct
```

### `broker_account`

Time-versioned (added during Phase 0, beyond original spec). On each sync:
- If `cash_usd` and `equity_usd` are within $0.01 of the current open row → `last_sync` updated in place, no new row.
- If either changes → open row closed (`effective_to = now`), new row inserted.

```
id | account_id | broker | mode | cash_usd | equity_usd | last_sync | effective_from | effective_to
```

`account_id` (Alpaca UUID) is stored on both `broker_account` and `positions_snapshot` to prevent data from different accounts being mixed if API keys change.

### Current targets (as of completion)

| Ticker | Target % | Band |
|---|---|---|
| VOO | 40 | [35, 45] |
| QQQ | 25 | [21, 29] |
| SCHD | 15 | [12, 18] |
| AMZN | 5 | [3, 8] |
| AAPL | 5 | [3, 8] |
| MSFT | 5 | [3, 8] |
| *(cash buffer)* | 5 | — |

---

## 4. Additions beyond original Phase 0 scope

These were small improvements made during implementation that reduce Phase 1 rework:

| Addition | Rationale |
|---|---|
| `account_id` on `broker_account` and `positions_snapshot` | Prevents data mixing if Alpaca account changes |
| `effective_from` / `effective_to` on `broker_account` | Consistent time-versioning across all mutable state |
| Deduplication in `load_targets.py` | Idempotent restarts — Docker CMD runs script on every container start |
| SQL extracted to `src/investor/sql/*.sql` | No inline SQL in Python; easier to audit and extend |
| Incremental schema migration in `db.py` | `ALTER TABLE … ADD COLUMN IF NOT EXISTS` so existing DB files upgrade without manual intervention |
| DuckDB CLI installed natively (arm64) | Developer convenience for inspecting live data |

---

## 5. Known issues and limitations

### Deduplication in `load_targets.py` has intermittent failures

On each service restart the Dockerfile CMD runs `load_targets.py`. Duplicate `target_allocation` rows have been observed in the database despite the dedup check. Root cause is unconfirmed — suspected float type returned by duckdb-engine differs from Python `float` in a way that defeats the `!=` comparison. The comparison was hardened to use `round(float(x), 6)` but has not been tested end-to-end under Docker yet.

**Recommended action for Phase 1:** Write a targeted test that seeds an in-memory DB, runs `load_targets.py` twice with no YAML change, and asserts exactly one set of open rows. Also consider changing the Dockerfile CMD to run `load_targets.py` only when the table is empty, and use `/admin/reload-targets` for subsequent updates.

### No recurring sync schedule

The Phase 0 scheduler fires once (30s after startup). There is no daily recurring job yet.

**Phase 1 action:** Replace `DateTrigger` with `CronTrigger("0 16 * * 1-5", timezone="America/New_York")` to sync after market close Monday–Friday.

### No bar data

`positions_snapshot` holds market values as reported by Alpaca (live/paper prices). There is no historical OHLCV data, no SMA/RSI computation, and no support/resistance levels. This is Phase 2 scope.

### No email

No notification layer. Phase 1 adds the daily portfolio snapshot email.

### `positions_snapshot` grows unbounded

Every sync appends rows. There is no retention policy. For daily syncs over 1–2 years this is manageable (~700 rows/ticker/year), but a periodic cleanup job should be considered by Phase 2.

### Single-writer DuckDB constraint

Running `uvicorn` and any CLI script simultaneously will raise a lock error. Currently documented but not enforced. Phase 5's multi-user requirement will require migrating transactional tables to Postgres.

---

## 6. Test coverage

| Test file | Tests | Coverage |
|---|---|---|
| `tests/test_config.py` | 8 | `Settings` env var loading, broker validation, `load_targets` YAML parsing, sum validation, band values, real `targets.yaml` |
| `tests/test_gap.py` | 8 | `compute_gap` with empty DB, fully unallocated, partial allocation, on-target, sort order, latest-snapshot-only, closed target exclusion, return type |

All 16 tests pass. No integration tests against a live Alpaca account.

---

## 7. Architecture decisions made in Phase 0

### BrokerAdapter is the only door to broker SDKs

No file outside `src/investor/brokers/` may import `alpaca` or any broker SDK. This is enforced by convention (no tooling guard yet). The adapter converts SDK types to `Account` and `Position` dataclasses at the boundary.

### `ALPACA_BASE_URL` is ignored

`alpaca-py`'s `TradingClient` routes paper/live via `paper=True`, not via URL. The env var is stored for human reference but not passed to the client. Documented in `.env.example` and in `CLAUDE.md`.

### Alembic skipped for Phase 0

Schema changes are handled by `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in `init_db()`. This is intentional for Phase 0 to avoid DuckDB DDL quirks with Alembic's autogenerate. Phase 1 should evaluate whether to introduce Alembic or continue with inline migrations.

### All timestamps UTC at rest

UTC is stored in all `TIMESTAMPTZ` columns. Conversion to `America/New_York` is done only for APScheduler cron triggers and display. This convention must be maintained in all future phases.

---

## 8. Environment and dependencies

- **Python:** 3.13 (project uses 3.12 in CLAUDE.md — actual runtime is 3.13)
- **Key runtime deps:** `fastapi`, `uvicorn[standard]`, `apscheduler>=3.10,<4`, `sqlalchemy>=2.0`, `duckdb-engine`, `alpaca-py`, `pydantic-settings`, `pytz`
- **Key dev deps:** `pytest`, `httpx`, `ruff`, `mypy`
- **Package manager:** `uv`
- **Container:** `python:3.13-slim`, single-stage after two `uv sync` calls

---

## 9. Recommended Phase 1 starting point

Based on the product plan and current state, Phase 1 should deliver:

1. **Daily scheduled sync** — replace `DateTrigger` with `CronTrigger` (Mon–Fri 16:00 ET)
2. **Daily portfolio snapshot email** — positions table + gap table, sent after sync
3. **Fix `load_targets.py` dedup** — add a test and resolve the intermittent duplicate-row bug
4. **Bar data backfill** — fetch 2 years of OHLCV from Alpaca, store as Parquet under `data/bars/`, queryable directly by DuckDB
5. **`price_bar` table / view** — makes bars accessible via SQLAlchemy for Phase 2 indicator work

The email in step 2 is the main user-visible output of Phase 1 and the most important thing to ship. Everything else unblocks Phase 2.

### Files Phase 1 will primarily touch

| File | Why |
|---|---|
| `src/investor/scheduler.py` | Replace `DateTrigger` with `CronTrigger` |
| `src/investor/jobs/` | Add `daily_report_job.py` |
| `src/investor/services/` | Add `email.py` (SMTP via `smtplib` + Jinja2) |
| `src/investor/config.py` | Add `SMTP_*`, `EMAIL_FROM`, `EMAIL_TO` settings (already in `.env.example`) |
| `scripts/` | Add `backfill_bars.py` |
| `src/investor/models.py` | Add `PriceBar` model (or keep bars Parquet-only and use DuckDB to query directly) |
| `tests/` | Add `test_email.py`, `test_daily_report.py` |
