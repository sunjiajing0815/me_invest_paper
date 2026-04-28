# Phase 0 Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 0 — Foundation  
**Completed:** 2026-04-28  
**Git tag:** `v0.0.1-phase-0` (branch: `main`; carryover fixes committed post-tag)

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
| `scripts/load_targets.py` | Seeds `target_allocation` from `config/targets.yaml`. Idempotent — compares SHA-256 hash of YAML content against `meta` table; skips write if unchanged. |
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

These were improvements made during and immediately after implementation that reduce Phase 1 rework:

| Addition | Rationale |
|---|---|
| `account_id` on `broker_account` and `positions_snapshot` | Prevents data mixing if Alpaca account changes |
| `effective_from` / `effective_to` on `broker_account` | Consistent time-versioning across all mutable state |
| Hash-based dedup in `load_targets.py` | SHA-256 of YAML content stored in `meta` table; idempotent across any number of restarts |
| `POST /admin/reload-targets` endpoint | Reload targets from running service without touching CLI |
| SQL extracted to `src/investor/sql/*.sql` | No inline SQL in Python; easier to audit and extend |
| Alembic for schema migrations | Replaces hand-rolled `ALTER TABLE`; enables safe schema evolution in Phases 2–4 |
| SQLite for OLTP / DuckDB for analytics | Native Alembic support; `--autogenerate` works; no stub required; DuckDB kept for Phase 1+ Parquet analytics |

---

## 5. Known issues and limitations

### No recurring sync schedule

The Phase 0 scheduler fires once (30s after startup). There is no daily recurring job yet.

**Phase 1 action:** Replace `DateTrigger` with `CronTrigger("0 16 * * 1-5", timezone="America/New_York")` to sync after market close Monday–Friday.

### No bar data

`positions_snapshot` holds market values as reported by Alpaca (live/paper prices). There is no historical OHLCV data, no SMA/RSI computation, and no support/resistance levels. This is Phase 2 scope.

### No email

No notification layer. Phase 1 adds the daily portfolio snapshot email.

### `positions_snapshot` grows unbounded

Every sync appends rows. There is no retention policy. For daily syncs over 1–2 years this is manageable (~700 rows/ticker/year), but a periodic cleanup job should be considered by Phase 2.

### Single-writer SQLite

SQLite serialises writes. Running `uvicorn` and a CLI script simultaneously against `investor.db` is safe for reads but concurrent writes will queue. Avoid heavy write scripts while the server is under load. Phase 5's multi-user requirement will require migrating to Postgres.

---

## 6. Test coverage

| Test file | Tests | Coverage |
|---|---|---|
| `tests/test_config.py` | 8 | `Settings` env var loading, broker validation, `load_targets` YAML parsing, sum validation, band values, real `targets.yaml` |
| `tests/test_gap.py` | 8 | `compute_gap` with empty DB, fully unallocated, partial allocation, on-target, sort order, latest-snapshot-only, closed target exclusion, return type |
| `tests/test_load_targets.py` | 5 | `load_targets_into_db` first load, idempotency (2 runs), correct open-row count after 3 runs, versioning closes old rows, open rows match new values |

All 21 tests pass on `sqlite:///:memory:`. No integration tests against a live Alpaca account.

---

## 7. Architecture decisions made in Phase 0

### BrokerAdapter is the only door to broker SDKs

No file outside `src/investor/brokers/` may import `alpaca` or any broker SDK. This is enforced by convention (no tooling guard yet). The adapter converts SDK types to `Account` and `Position` dataclasses at the boundary.

### `ALPACA_BASE_URL` removed

`alpaca-py`'s `TradingClient` routes paper/live via `paper=True`, not via URL override. The env var was removed from `Settings` and `.env.example` entirely.

### Alembic adopted (Phase 0 carryover)

Schema migrations are managed by Alembic. `init_db()` calls `alembic upgrade head` on every startup. `render_as_batch=True` is set in `migrations/env.py` for future column changes on SQLite. `--autogenerate` is available (no DuckDB catalog errors).

### SQLite for OLTP, DuckDB for analytics (Phase 0 carryover)

Transactional tables (`target_allocation`, `broker_account`, `meta`, `positions_snapshot`) are stored in SQLite (`data/investor.db`). DuckDB is retained as a direct Python dependency for Phase 1+ Parquet-based analytical queries.

### All timestamps UTC at rest

UTC is stored in all `TIMESTAMPTZ` columns. Conversion to `America/New_York` is done only for APScheduler cron triggers and display. This convention must be maintained in all future phases.

---

## 8. Environment and dependencies

- **Python:** 3.12
- **Key runtime deps:** `fastapi`, `uvicorn[standard]`, `apscheduler>=3.10,<4`, `sqlalchemy>=2.0`, `alembic>=1.13`, `duckdb>=1.5.2` (analytics only), `alpaca-py`, `pydantic-settings`, `pytz`
- **Key dev deps:** `pytest`, `httpx`, `ruff`, `mypy`
- **Package manager:** `uv`
- **Container:** `python:3.12-slim`

---

## 9. Recommended Phase 1 starting point

Based on the product plan and current state, Phase 1 should deliver:

1. **Daily scheduled sync** — replace `DateTrigger` with `CronTrigger` (Mon–Fri 16:00 ET)
2. **Daily portfolio snapshot email** — positions table + gap table, sent after sync
3. **Bar data backfill** — fetch 2 years of OHLCV from Alpaca, store as Parquet under `data/bars/`
4. **DuckDB analytical layer** — query Parquet bars directly via `import duckdb` (not SQLAlchemy); expose computed indicators to Phase 2

The email in step 2 is the main user-visible output of Phase 1 and the most important thing to ship. Everything else unblocks Phase 2.

### Files Phase 1 will primarily touch

| File | Why |
|---|---|
| `src/investor/scheduler.py` | Replace `DateTrigger` with `CronTrigger` |
| `src/investor/jobs/` | Add `daily_report_job.py` |
| `src/investor/services/` | Add `email.py` (SMTP via `smtplib` + Jinja2) |
| `src/investor/config.py` | Add `SMTP_*`, `EMAIL_FROM`, `EMAIL_TO` settings (already in `.env.example`) |
| `scripts/` | Add `backfill_bars.py` |
| `src/investor/models.py` | Add `PriceBar` model if needed (bars may stay Parquet-only, queried via DuckDB directly) |
| `tests/` | Add `test_email.py`, `test_daily_report.py` |
