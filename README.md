# Investor Assistant — Phase 1

A self-hosted, **suggest-only** portfolio assistant for long-term US-equity investors. Pulls positions from Alpaca, compares them against a YAML-defined target allocation, identifies allocation drift, and sends a daily portfolio email. The system never places orders — execution is always manual in the broker's UI.

**Current phase:** 1 — Portfolio Email & Bar Backfill  
**Status:** Code complete. Awaiting 5-consecutive-day email streak before tagging `v0.1.0-phase-1`.

---

## Quick start

### Prerequisites

- Python 3.12 + [uv](https://docs.astral.sh/uv/)
- Docker Desktop (for containerised run)
- An [Alpaca](https://alpaca.markets) paper account (free)
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) enabled (for email reports)

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env — set ALPACA_* and SMTP_* variables
```

Required variables (see `.env.example` for the full list):

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `SQLITE_PATH` | Path to SQLite file, e.g. `data/investor.db` |
| `TARGETS_PATH` | Path to YAML targets, e.g. `config/targets.yaml` |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | e.g. `587` |
| `SMTP_USER` | Gmail address |
| `SMTP_APP_PASSWORD` | Gmail App Password |
| `EMAIL_FROM` | Sender address |
| `EMAIL_TO` | Recipient address |

### 2. Configure target allocation

Edit `config/targets.yaml`. Percentages must sum to `100 - cash_buffer_pct`:

```yaml
watchlist: [VOO, QQQ, SCHD, AAPL, MSFT, AMZN]
targets:
  VOO:  { pct: 40, band: [35, 45] }
  QQQ:  { pct: 25, band: [21, 29] }
  SCHD: { pct: 15, band: [12, 18] }
  AMZN: { pct: 5,  band: [3,  8]  }
  AAPL: { pct: 5,  band: [3,  8]  }
  MSFT: { pct: 5,  band: [3,  8]  }
cash_buffer_pct: 5
```

---

## Running locally (no Docker)

```bash
# Install dependencies
uv sync

# Apply DB migrations
uv run alembic upgrade head

# Seed target allocation into the database
uv run python scripts/load_targets.py

# Pull current positions from Alpaca
uv run python scripts/sync_positions.py

# (Optional) Backfill 2 years of OHLCV bar data
uv run python scripts/backfill_bars.py

# Start the API server
uv run uvicorn src.investor.main:app --reload --port 8000
```

The API is available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

The scheduler starts automatically with the server and fires the daily report job Mon–Fri at 16:15 America/New_York (with a 30-minute misfire grace window).

### Updating targets

1. Edit `config/targets.yaml`
2. Call `POST /admin/reload-targets` — skips the write if content is unchanged, otherwise closes old rows and inserts new ones:
   ```bash
   curl -X POST localhost:8000/admin/reload-targets
   # → {"status":"ok","result":"updated"}
   ```
   Or run the CLI script directly (stop uvicorn first to avoid concurrent writes):
   ```bash
   uv run python scripts/load_targets.py
   ```

---

## Running with Docker

```bash
# First start
docker compose up --build -d

# View logs
docker compose logs -f app

# Rebuild after code changes
docker compose up --build -d

# Stop
docker compose down
```

On first deploy, seed the target allocation before or after starting:

```bash
# Option A: seed while server is stopped
docker compose run --rm app uv run python scripts/load_targets.py
docker compose up -d

# Option B: seed via the running server's reload endpoint
docker compose up -d
curl -X POST localhost:8000/admin/reload-targets
```

The SQLite file and bar Parquet files are stored in `./data/` which is bind-mounted, so data persists across restarts.

### Full reset

```bash
docker compose down
rm data/investor.db
rm -rf data/bars/
docker compose up --build -d
```

---

## API endpoints

### `GET /health`

Returns service status, broker, last sync timestamp, and number of active targets.

```json
{
  "status": "ok",
  "broker": "alpaca_paper",
  "last_sync_ts": "2026-04-28T12:00:00+00:00",
  "target_count": 6
}
```

### `GET /positions`

Latest snapshot per ticker, ordered by portfolio weight descending.

```json
[
  {
    "ticker": "VOO",
    "ts": "2026-04-28T12:00:00+00:00",
    "qty": 10.0,
    "avg_cost": 480.50,
    "market_value": 4900.00,
    "weight_pct": 39.2
  }
]
```

### `GET /gap`

Allocation gap between current holdings and targets, ordered by absolute gap descending. Positive `gap_pct` means underweight (buy); negative means overweight (trim). `band_status` is `"under"`, `"in_band"`, or `"over"`.

```json
[
  {
    "ticker": "VOO",
    "current_pct": 39.2,
    "target_pct": 40.0,
    "gap_pct": 0.8,
    "gap_usd": 96.50,
    "band_status": "in_band"
  }
]
```

### `GET /drift`

Same as `/gap` but returns only tickers where `band_status != "in_band"` — i.e. positions that are outside their rebalance band and require attention.

```json
[
  {
    "ticker": "SCHD",
    "current_pct": 10.1,
    "target_pct": 15.0,
    "gap_pct": 4.9,
    "gap_usd": 490.00,
    "band_status": "under"
  }
]
```

### `POST /admin/run-sync`

Triggers an immediate position sync from the broker. Runs synchronously and returns when complete.

```json
{ "status": "ok", "message": "Sync completed" }
```

### `POST /admin/run-daily-report`

Manually triggers the full daily report job (sync → compose → render → email). Useful for testing email delivery without waiting for the scheduler.

```json
{ "status": "ok" }
```

### `POST /admin/reload-targets`

Reloads target allocations from `config/targets.yaml`. Idempotent — compares a SHA-256 hash of the file content against what's stored; skips any DB write if the file hasn't changed.

```json
{ "status": "ok", "result": "updated" }
```

`result` is `"updated"` when new rows were written, `"unchanged"` when the file matched the stored hash.

Interactive API docs: `http://localhost:8000/docs`

---

## Daily email report

The scheduler fires Mon–Fri at 16:15 America/New_York (after market close). Each email contains:

| Section | Content |
|---|---|
| Header | Date, equity, cash, broker/mode |
| Drift alerts | Yellow banner — only shown when tickers are outside their rebalance band |
| Allocation table | Ticker, qty, avg cost, market value, current %, target %, gap %, band status (✓ / ⚠ under / ⚠ over) |
| Gap summary | Top 3 underweight (buy) + top 3 overweight (trim) |
| Footer | "No orders are placed automatically." |

Both HTML (inline styles, no external images) and plain-text versions are sent as a MIME multipart email.

Subject line: `Portfolio — YYYY-MM-DD (equity $XX,XXX)`

To trigger immediately without waiting for the scheduler:
```bash
curl -X POST localhost:8000/admin/run-daily-report
```

---

## Bar data (OHLCV)

Phase 1 backfills 2 years of daily OHLCV bars per ticker and stores them as Parquet files. These are the foundation for Phase 2 technical indicators (SMA, RSI, support/resistance).

```bash
# One-time backfill (fetches ~500 trading days per ticker from Alpaca IEX)
uv run python scripts/backfill_bars.py

# Daily update (appends yesterday+today, deduplicates by timestamp)
uv run python scripts/update_bars.py
```

Files are written to `data/bars/<TICKER>.parquet` (e.g. `data/bars/VOO.parquet`). Each file is ~40 KB; 6 tickers ≈ 250 KB total. The `data/bars/` directory is bind-mounted in Docker and gitignored.

Bars are queryable directly via DuckDB:

```python
import duckdb
conn = duckdb.connect()
conn.execute("SELECT * FROM read_parquet('data/bars/*.parquet') ORDER BY timestamp DESC LIMIT 10").fetchdf()
```

---

## Data models

All transactional tables are stored in a single SQLite file at `./data/investor.db`.

### `target_allocation`

Time-versioned target allocation. Rows are never updated in place — when targets change, previous rows are closed (`effective_to` set to now) and new rows inserted.

| Column | Type | Description |
|---|---|---|
| `id` | integer | Primary key |
| `ticker` | varchar | e.g. `VOO` |
| `target_pct` | double | Target weight, e.g. `40.0` |
| `band_low_pct` | double | Lower rebalance band |
| `band_high_pct` | double | Upper rebalance band |
| `effective_from` | timestamptz | When this target became active |
| `effective_to` | timestamptz | When superseded (NULL = current) |

### `positions_snapshot`

One row per ticker per sync. Every sync appends new rows — no updates in place. The `/positions` and `/gap` endpoints select the latest row per ticker via a window function.

| Column | Type | Description |
|---|---|---|
| `id` | integer | Primary key |
| `ts` | timestamptz | Sync timestamp |
| `ticker` | varchar | e.g. `VOO` |
| `qty` | double | Shares held |
| `avg_cost` | double | Average entry price (USD) |
| `market_value` | double | Current market value (USD) |
| `weight_pct` | double | Portfolio weight at sync time (% of total equity incl. cash) |

### `broker_account`

Time-versioned account state. On each sync: if cash and equity are within $0.01 of the current open row, only `last_sync` is updated. If either changes, the open row is closed and a new row inserted.

| Column | Type | Description |
|---|---|---|
| `id` | integer | Primary key |
| `broker` | varchar | e.g. `alpaca` |
| `mode` | varchar | `paper` or `live` |
| `cash_usd` | double | Cash balance |
| `equity_usd` | double | Total portfolio equity |
| `last_sync` | timestamptz | Most recent sync for this row |
| `effective_from` | timestamptz | When this account state began |
| `effective_to` | timestamptz | When superseded (NULL = current) |

### `meta`

Key/value store for app-level metadata. Currently stores the SHA-256 hash of the last-loaded `targets.yaml` to enable idempotent reloads.

| Column | Type | Description |
|---|---|---|
| `key` | varchar (PK) | e.g. `targets_yaml_hash` |
| `value` | varchar | Stored value |

---

## Inspecting the database

Use the standard `sqlite3` CLI (built into macOS/Linux). The server and CLI can read the file simultaneously — SQLite allows multiple concurrent readers.

```bash
sqlite3 data/investor.db
```

```sql
-- current targets
SELECT ticker, target_pct, band_low_pct, band_high_pct
FROM target_allocation WHERE effective_to IS NULL;

-- latest positions
SELECT ticker, qty, market_value, weight_pct
FROM positions_snapshot
WHERE (ticker, ts) IN (SELECT ticker, MAX(ts) FROM positions_snapshot GROUP BY ticker);

-- current account state
SELECT cash_usd, equity_usd, last_sync
FROM broker_account WHERE effective_to IS NULL;

-- stored YAML hash
SELECT value FROM meta WHERE key = 'targets_yaml_hash';
```

---

## Project layout

```
src/investor/
  main.py             FastAPI app + lifespan (startup/shutdown, app.state wiring)
  config.py           pydantic-settings + targets.yaml loader/validator
  db.py               SQLite engine, session factory, Alembic migration runner
  models.py           SQLAlchemy ORM models
  scheduler.py        APScheduler bootstrap (CronTrigger Mon–Fri 16:15 ET)
  queries.py          SQL registry — loads named .sql files at import time
  sql/                Named SQL files
    gap_allocation.sql
    positions_latest.sql
    account_last_sync.sql
    targets_active_count.sql
  brokers/
    base.py           BrokerAdapter Protocol + Account/Position dataclasses
    alpaca.py         AlpacaAdapter (wraps alpaca-py TradingClient + StockHistoricalDataClient)
  services/
    snapshot.py       Pull + persist positions and account state
    gap.py            Compute allocation gap from DB (returns GapRow frozen dataclasses)
    targets.py        Hash-based idempotent target loader
    daily_report.py   compose_daily_report() — reads DB, returns DailyReport dataclass
    email.py          EmailSender Protocol, SMTPEmailer (STARTTLS), FakeEmailer (tests)
    render.py         Jinja2 template renderer (FileSystemLoader on templates/)
  jobs/
    sync.py           APScheduler job wrapper for position sync
    daily_report.py   run_daily_report() — orchestrates sync → compose → render → send
config/
  targets.yaml        Target allocation (hand-edited)
templates/
  daily_report.html.j2  HTML email template (inline styles, no external images)
  daily_report.txt.j2   Plain-text email template
scripts/
  load_targets.py     Seed/update target_allocation from targets.yaml
  sync_positions.py   One-shot position sync from broker
  backfill_bars.py    Fetch 2 years of OHLCV from Alpaca, write data/bars/<TICKER>.parquet
  update_bars.py      Append yesterday+today bars to existing Parquet, deduplicate
migrations/           Alembic revisions
data/
  investor.db         SQLite file — bind-mounted in Docker, gitignored
  bars/               Parquet bar files — bind-mounted in Docker, gitignored
    VOO.parquet
    QQQ.parquet
    ...
tests/
  test_config.py          Settings + YAML loader tests (8 tests)
  test_gap.py             Gap computation + band_status tests (10 tests)
  test_load_targets.py    Hash-based target dedup tests (5 tests)
  test_email.py           FakeEmailer + SMTPEmailer tests (3 tests)
  test_daily_report.py    DailyReport composer + session-close regression (3 tests)
  test_integration_alpaca.py  Full chain against live Alpaca paper account (1 test, skips without API keys)
docs/adr/             Architecture Decision Records
  0001-broker-adapter-abstraction.md
  0002-three-tier-storage-architecture.md
  0003-schema-migrations-alembic-sqlite.md
  0004-bar-storage.md
  0005-email-failure-policy.md
```

---

## Development

```bash
uv sync
uv run pytest                        # 29 unit tests + 1 integration (skipped without API keys)
uv run pytest -m "not integration"   # unit tests only
uv run ruff check --fix
uv run mypy src/
```
