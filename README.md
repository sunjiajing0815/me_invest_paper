# Investor Assistant — Phase 0 MVP

A self-hosted, **suggest-only** portfolio assistant for long-term US-equity investors. Pulls positions from Alpaca, compares them against a YAML-defined target allocation, and exposes the gap via a REST API. The system never places orders — execution is always manual in the broker's UI.

---

## Quick start

### Prerequisites

- Python 3.12 + [uv](https://docs.astral.sh/uv/)
- Docker Desktop (for containerised run)
- An [Alpaca](https://alpaca.markets) paper account (free)

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env — set ALPACA_API_KEY and ALPACA_SECRET_KEY
```

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

# Seed target allocation into the database
uv run python scripts/load_targets.py

# Pull current positions from Alpaca
uv run python scripts/sync_positions.py

# Start the API server
uv run uvicorn src.investor.main:app --reload --port 8000
```

The API is now available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

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

The SQLite file is stored in `./data/` which is bind-mounted, so data persists across restarts.

### Full reset

```bash
docker compose down
rm data/investor.db
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

Allocation gap between current holdings and targets, ordered by absolute gap descending. Positive `gap_pct` means underweight (buy); negative means overweight (trim).

```json
[
  {
    "ticker": "VOO",
    "current_pct": 39.2,
    "target_pct": 40.0,
    "gap_pct": 0.8,
    "gap_usd": 96.50
  }
]
```

### `POST /admin/run-sync`

Triggers an immediate position sync from the broker. Runs synchronously and returns when complete.

```json
{ "status": "ok", "message": "Sync completed" }
```

### `POST /admin/reload-targets`

Reloads target allocations from `config/targets.yaml`. Idempotent — compares a SHA-256 hash of the file content against what's stored; skips any DB write if the file hasn't changed.

```json
{ "status": "ok", "result": "updated" }
```

`result` is `"updated"` when new rows were written, `"unchanged"` when the file matched the stored hash.

Interactive API docs: `http://localhost:8000/docs`

---

## Data models

All tables are stored in a single SQLite file at `./data/investor.db`.

### `target_allocation`

Time-versioned target allocation. Rows are never updated in place — when targets change, previous rows are closed (`effective_to` set to now) and new rows inserted. If `load_targets.py` detects no change, it skips the write entirely.

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
| `account_id` | varchar | Broker account UUID |
| `ts` | timestamptz | Sync timestamp |
| `ticker` | varchar | e.g. `VOO` |
| `qty` | double | Shares held |
| `avg_cost` | double | Average entry price (USD) |
| `market_value` | double | Current market value (USD) |
| `weight_pct` | double | Portfolio weight at sync time |

### `broker_account`

Time-versioned account state. On each sync: if cash and equity are within $0.01 of the current open row, only `last_sync` is updated. If either changes, the open row is closed and a new row inserted.

| Column | Type | Description |
|---|---|---|
| `id` | integer | Primary key |
| `account_id` | varchar | Broker account UUID |
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
SELECT account_id, cash_usd, equity_usd, last_sync
FROM broker_account WHERE effective_to IS NULL;

-- stored YAML hash
SELECT value FROM meta WHERE key = 'targets_yaml_hash';
```

---

## Project layout

```
src/investor/
  main.py             FastAPI app + lifespan (startup/shutdown)
  config.py           pydantic-settings + targets.yaml loader/validator
  db.py               SQLite engine, session factory, Alembic migration runner
  models.py           SQLAlchemy ORM models
  scheduler.py        APScheduler bootstrap (initial sync 30s after start)
  queries.py          SQL registry — loads named .sql files at import time
  sql/                Named SQL files
    gap_allocation.sql
    positions_latest.sql
    account_last_sync.sql
    targets_active_count.sql
  brokers/
    base.py           BrokerAdapter Protocol + Account/Position dataclasses
    alpaca.py         AlpacaAdapter (wraps alpaca-py TradingClient)
  services/
    snapshot.py       Pull + persist positions and account state
    gap.py            Compute allocation gap from DB
    targets.py        Hash-based idempotent target loader
  jobs/
    sync.py           APScheduler job wrapper for position sync
config/
  targets.yaml        Target allocation (hand-edited)
scripts/
  load_targets.py     Seed/update target_allocation from targets.yaml
  sync_positions.py   One-shot position sync from broker
migrations/           Alembic revisions
data/                 SQLite file — bind-mounted in Docker, gitignored
tests/
  test_config.py      Settings + YAML loader tests (8 tests)
  test_gap.py         Gap computation tests using in-memory SQLite (8 tests)
  test_load_targets.py Hash-based target dedup tests (5 tests)
```

---

## Development

```bash
uv sync
uv run pytest
uv run ruff check --fix
uv run mypy src/
```
