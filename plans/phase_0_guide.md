# Phase 0 — Foundation: Step-by-Step Guide

**Goal:** End with `docker compose up` running on your Mac, a working FastAPI `/health` and `/gap` endpoint, an APScheduler instance wired up (but not yet on a daily cron), a DuckDB file containing your real Alpaca paper account positions, and a CLI command that prints your current allocation vs. target.

**Out of scope for Phase 0** (these are Phase 1+): daily cron schedule, email delivery, drift band alerts, multi-day snapshot history analysis, S/R levels, news, LLM.

**Time budget:** 3–5 evenings (12–18 focused hours).

**Definition of done:** the Phase 0 smoke-test checklist at the end of this document passes end-to-end, twice, on consecutive days.

---

## 0. Pre-flight checklist (do this before writing any code)

These are operational steps that block development if missed. ~30 minutes.

- [ ] Alpaca paper account created at `https://app.alpaca.markets/signup` → choose **Trading API**.
- [ ] Generate paper API key + secret in the dashboard. Store in 1Password (or any password manager) labelled `Alpaca Paper — Investor Assistant`.
- [ ] Anthropic API key generated at `https://console.anthropic.com`. Set a $10 monthly spend cap. Save in 1Password. (Not used in Phase 0, but we'll bake the env var in now to avoid churn.)
- [ ] Mac: Docker Desktop installed and running; verify with `docker run hello-world`.
- [ ] `uv` installed: `brew install uv`. Verify with `uv --version`.
- [ ] Python 3.12 available: `uv python install 3.12`.
- [ ] Private GitHub repo created — suggested name `investor-assistant`. Cloned locally to `~/code/investor-assistant`.
- [ ] Gmail App Password generated at `https://myaccount.google.com/apppasswords` (you'll need it for Phase 1, but generate now while you remember).

---

## 1. Repo skeleton

Lay out the directories first. Empty files are fine — we'll fill them in subsequent steps. This skeleton anticipates Phases 1–4 so you're not refactoring directories every week.

```
investor-assistant/
├── .gitignore
├── .dockerignore
├── .env.example
├── .pre-commit-config.yaml
├── CLAUDE.md
├── Dockerfile
├── README.md
├── alembic.ini
├── docker-compose.yml
├── pyproject.toml
├── config/
│   └── targets.yaml
├── data/                       # bind-mounted; gitignored
│   └── .gitkeep
├── docs/
│   └── adr/
│       └── 0001-rebalance-bands.md
├── migrations/
│   ├── env.py
│   └── versions/
├── scripts/
│   ├── sync_positions.py        # one-off CLI: pull + persist
│   └── show_gap.py              # one-off CLI: pretty-print gap
├── src/
│   └── investor/
│       ├── __init__.py
│       ├── main.py              # FastAPI app + lifespan
│       ├── config.py            # pydantic settings + targets.yaml loader
│       ├── db.py                # DuckDB engine + session factory
│       ├── models.py            # SQLAlchemy ORM models
│       ├── scheduler.py         # APScheduler bootstrap
│       ├── brokers/
│       │   ├── __init__.py
│       │   ├── base.py          # BrokerAdapter Protocol + dataclasses
│       │   └── alpaca.py        # AlpacaAdapter
│       ├── services/
│       │   ├── __init__.py
│       │   ├── snapshot.py      # take + persist a positions snapshot
│       │   └── gap.py           # compute gap vs. effective targets
│       └── jobs/
│           ├── __init__.py
│           └── sync.py          # APScheduler job: calls snapshot service
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_config.py
    └── test_gap.py
```

`.gitignore` essentials: `.env`, `data/*.duckdb`, `data/*.parquet`, `__pycache__/`, `.venv/`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`.

`.dockerignore`: same as `.gitignore` plus `tests/`, `docs/`, `.git/`.

---

## 2. Project bootstrap

```bash
cd ~/code/investor-assistant
uv init --python 3.12
```

Edit `pyproject.toml` and add dependencies:

```toml
[project]
name = "investor-assistant"
version = "0.0.1"
requires-python = ">=3.12"
dependencies = [
    "alpaca-py>=0.30",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "apscheduler>=3.10",
    "duckdb>=1.1",
    "duckdb-engine>=0.13",
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "pyyaml>=6.0",
    "pandas>=2.2",
    "rich>=13.9",
    "anthropic>=0.39",
    "python-dotenv>=1.0",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
    "ruff>=0.7",
    "mypy>=1.13",
    "pre-commit>=4.0",
]

[tool.ruff]
line-length = 100
target-version = "py312"
[tool.ruff.lint]
select = ["E", "F", "I", "N", "B", "UP", "SIM"]

[tool.mypy]
python_version = "3.12"
strict = true
ignore_missing_imports = true
```

Then:

```bash
uv sync
uv run pre-commit install
```

---

## 3. Configuration

### `.env.example`

```bash
# Broker
BROKER=alpaca_paper
ALPACA_API_KEY=replace-me
ALPACA_SECRET_KEY=replace-me
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Storage
DUCKDB_PATH=./data/investor.duckdb
TARGETS_PATH=./config/targets.yaml

# Reserved for later phases
ANTHROPIC_API_KEY=
SMTP_HOST=
SMTP_USER=
SMTP_APP_PASSWORD=
EMAIL_FROM=
EMAIL_TO=

# Operational
LOG_LEVEL=INFO
TZ=America/New_York
```

Copy to `.env`, fill in real values, `chmod 600 .env`.

### `config/targets.yaml`

A starter file you can edit anytime. Put 3–5 tickers; you can expand later.

```yaml
watchlist: [VOO, QQQ, SCHD, AAPL, MSFT]
targets:
  VOO:  { pct: 40, band: [35, 45] }
  QQQ:  { pct: 25, band: [21, 29] }
  SCHD: { pct: 15, band: [12, 18] }
  AAPL: { pct: 10, band: [7,  13] }
  MSFT: { pct: 10, band: [7,  13] }
cash_buffer_pct: 5
```

### `src/investor/config.py`

A `pydantic-settings` class that loads env vars and a small helper that loads `targets.yaml` and validates that `pct` values sum to `100 - cash_buffer_pct ± 0.5`.

Key behaviours:
- Fail fast on startup if `ALPACA_API_KEY` is missing or `targets.yaml` doesn't validate.
- Expose `settings.broker` as a string enum (`alpaca_paper`, `alpaca_live`, `moomoo`) so the broker factory can pick the right adapter.

---

## 4. Database: DuckDB + SQLAlchemy + Alembic

### `src/investor/db.py`

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def make_engine(duckdb_path: str):
    return create_engine(f"duckdb:///{duckdb_path}", future=True)

SessionLocal = None  # set by main.py at startup
```

### `src/investor/models.py`

For Phase 0, only three tables. The rest get added in later phases via Alembic migrations — don't pre-build what you don't use.

```python
from datetime import datetime
from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase

class Base(DeclarativeBase): pass

class TargetAllocation(Base):
    __tablename__ = "target_allocation"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str]
    target_pct: Mapped[float]
    band_low_pct: Mapped[float]
    band_high_pct: Mapped[float]
    effective_from: Mapped[datetime]
    effective_to: Mapped[datetime | None] = mapped_column(default=None)

class PositionsSnapshot(Base):
    __tablename__ = "positions_snapshot"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime]
    ticker: Mapped[str]
    qty: Mapped[float]
    avg_cost: Mapped[float]
    market_value: Mapped[float]
    weight_pct: Mapped[float]

class BrokerAccount(Base):
    __tablename__ = "broker_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str]                 # alpaca | moomoo | ibkr
    mode: Mapped[str]                   # paper | live
    cash_usd: Mapped[float]
    equity_usd: Mapped[float]
    last_sync: Mapped[datetime]
```

### Alembic init

```bash
uv run alembic init migrations
```

Edit `alembic.ini` so `sqlalchemy.url` is read from `DUCKDB_PATH` env var (override in `migrations/env.py`).

Generate first migration:

```bash
uv run alembic revision --autogenerate -m "phase0 initial schema"
uv run alembic upgrade head
```

You should see `data/investor.duckdb` appear and contain the three tables. Sanity-check:

```bash
uv run python -c "import duckdb; print(duckdb.connect('data/investor.duckdb').sql('SHOW TABLES'))"
```

### Seed targets from YAML (one-off helper)

A small script `scripts/load_targets.py` that reads `config/targets.yaml` and writes target rows with `effective_from = now`. Re-running it closes previous rows (`effective_to = now`) and inserts new — this is the v0 of Phase 4.5's target-versioning behaviour. Cheap to do now; saves a refactor later.

---

## 5. BrokerAdapter interface + Alpaca implementation

### `src/investor/brokers/base.py`

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

@dataclass(frozen=True)
class Account:
    cash_usd: float
    equity_usd: float
    buying_power_usd: float
    as_of: datetime

@dataclass(frozen=True)
class Position:
    ticker: str
    qty: float
    avg_cost: float
    market_value: float
    as_of: datetime

class BrokerAdapter(Protocol):
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
```

### `src/investor/brokers/alpaca.py`

Wrap `alpaca-py`'s `TradingClient` so the rest of the app never imports Alpaca SDK types. The adapter converts Alpaca's `TradeAccount` and `Position` objects into your dataclasses.

```python
from alpaca.trading.client import TradingClient
from .base import Account, Position
from datetime import datetime, UTC

class AlpacaAdapter:
    def __init__(self, key: str, secret: str, paper: bool):
        self._client = TradingClient(key, secret, paper=paper)

    def get_account(self) -> Account:
        a = self._client.get_account()
        return Account(
            cash_usd=float(a.cash),
            equity_usd=float(a.equity),
            buying_power_usd=float(a.buying_power),
            as_of=datetime.now(UTC),
        )

    def get_positions(self) -> list[Position]:
        now = datetime.now(UTC)
        return [
            Position(
                ticker=p.symbol,
                qty=float(p.qty),
                avg_cost=float(p.avg_entry_price),
                market_value=float(p.market_value),
                as_of=now,
            )
            for p in self._client.get_all_positions()
        ]
```

A small factory `brokers/__init__.py::make_adapter(settings)` returns the right adapter based on `settings.broker`. Right now it only knows `alpaca_paper` / `alpaca_live` — `moomoo` raises `NotImplementedError`. That's fine; we'll fill it in later.

---

## 6. Snapshot service

### `src/investor/services/snapshot.py`

Single function:

```python
def take_snapshot(adapter: BrokerAdapter, session: Session) -> int:
    """Pull positions+account, write rows in one transaction. Returns row count."""
    account = adapter.get_account()
    positions = adapter.get_positions()
    total_equity = account.equity_usd

    rows = []
    for p in positions:
        weight_pct = (p.market_value / total_equity * 100) if total_equity else 0.0
        rows.append(PositionsSnapshot(
            ts=p.as_of, ticker=p.ticker, qty=p.qty,
            avg_cost=p.avg_cost, market_value=p.market_value,
            weight_pct=weight_pct,
        ))

    session.add_all(rows)
    session.add(BrokerAccount(
        broker=settings.broker.split("_")[0],
        mode="paper" if "paper" in settings.broker else "live",
        cash_usd=account.cash_usd, equity_usd=account.equity_usd,
        last_sync=account.as_of,
    ))
    session.commit()
    return len(rows)
```

Notes:
- For Phase 0 your paper account probably has zero positions. That's fine — `take_snapshot` should still write a `broker_account` row so `/health` has something to show. Add a couple of fake positions in the Alpaca paper UI to make this more interesting (e.g., buy 10 VOO in paper).

---

## 7. Gap service

### `src/investor/services/gap.py`

The gap query is a single SQL statement. DuckDB's window functions make this clean:

```python
GAP_SQL = """
WITH latest AS (
  SELECT ticker, weight_pct, market_value,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
  FROM positions_snapshot
),
current AS (SELECT ticker, weight_pct, market_value FROM latest WHERE rn = 1),
account AS (SELECT equity_usd FROM broker_account ORDER BY last_sync DESC LIMIT 1),
targets AS (
  SELECT ticker, target_pct, band_low_pct, band_high_pct
  FROM target_allocation
  WHERE effective_to IS NULL
)
SELECT
  t.ticker,
  COALESCE(c.weight_pct, 0)        AS current_pct,
  t.target_pct                     AS target_pct,
  t.target_pct - COALESCE(c.weight_pct, 0) AS gap_pct,
  (t.target_pct - COALESCE(c.weight_pct, 0)) / 100 * a.equity_usd AS gap_usd
FROM targets t
LEFT JOIN current c USING (ticker)
CROSS JOIN account a
ORDER BY ABS(gap_pct) DESC
"""
```

Wrap in a Python function `compute_gap(session) -> list[GapRow]`.

**Phase 0 simplification:** no share-count math (that needs prices, which we'll add when Phase 2 introduces bars). `gap_usd` is enough to validate the math is correct.

---

## 8. FastAPI app

### `src/investor/main.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # init: db engine, scheduler
    init_db()
    scheduler = make_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan, title="Investor Assistant")

@app.get("/health")
def health(): ...
@app.get("/positions")
def positions(): ...
@app.get("/gap")
def gap(): ...
```

The three endpoints for Phase 0:
- `GET /health` → `{ status, broker, db_path, last_sync_ts, target_count }`
- `GET /positions` → latest snapshot rows
- `GET /gap` → gap rows from the SQL above

No auth in Phase 0 — bind to `127.0.0.1` only and don't expose the port outside the host. We'll add auth in Phase 5.

---

## 9. APScheduler

### `src/investor/scheduler.py`

For Phase 0, do **not** wire a recurring schedule. Instead:

- Build the scheduler infrastructure (so Phase 1 only adds jobs, not framework).
- Register a single one-off job that runs `take_snapshot` 30 seconds after startup. This validates the wiring without committing to a cron yet.
- Expose `POST /admin/run-sync` that triggers the same job ad-hoc — handy for development.

```python
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta, UTC

def make_scheduler():
    sched = BackgroundScheduler(timezone="America/New_York")
    sched.add_job(
        run_sync_once,
        trigger=DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=30)),
        id="phase0_initial_sync",
        replace_existing=True,
    )
    return sched
```

`run_sync_once` opens a session, calls `take_snapshot`, logs the row count.

Phase 1 will replace the `DateTrigger` with `CronTrigger(day_of_week='mon-fri', hour=9, minute=15)` etc.

---

## 10. CLI scripts

### `scripts/sync_positions.py`

Standalone — does **not** import FastAPI. Useful when you want to test the broker connection without booting the whole stack.

```python
if __name__ == "__main__":
    settings = Settings()
    adapter = make_adapter(settings)
    with session_scope() as s:
        n = take_snapshot(adapter, s)
        print(f"wrote {n} positions snapshots + 1 broker_account row")
```

### `scripts/show_gap.py`

```python
from rich.table import Table
from rich.console import Console

if __name__ == "__main__":
    rows = compute_gap(...)
    t = Table(title="Allocation Gap")
    t.add_column("Ticker"); t.add_column("Current %", justify="right")
    t.add_column("Target %", justify="right"); t.add_column("Gap %", justify="right")
    t.add_column("Gap $", justify="right")
    for r in rows:
        t.add_row(r.ticker, f"{r.current_pct:.2f}", f"{r.target_pct:.2f}",
                  f"{r.gap_pct:+.2f}", f"${r.gap_usd:+,.0f}")
    Console().print(t)
```

Running `uv run scripts/show_gap.py` should give you a nicely formatted table immediately after running `sync_positions.py`. This is your **Phase 0 motivational milestone** — a real number you computed from your real broker account.

---

## 11. Dockerfile + docker-compose.yml

### `Dockerfile`

```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "src.investor.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### `docker-compose.yml`

```yaml
services:
  app:
    build: .
    ports: ["127.0.0.1:8000:8000"]   # bind to localhost only
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./config:/app/config:ro
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
```

Same image runs identically on your Mac (Docker Desktop) or any Linux VPS later.

### Mac-native dev (no Docker)

For tight iteration loops, run without Docker:

```bash
uv run alembic upgrade head
uv run python scripts/sync_positions.py
uv run python scripts/show_gap.py
uv run uvicorn src.investor.main:app --reload --port 8000
```

The container and the Mac-native run share the same `data/investor.duckdb` file, so they're interchangeable.

---

## 12. Smoke-test checklist (Phase 0 done when all green)

Run this top-to-bottom on a clean machine state. Both Mac-native and Docker should pass.

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run alembic upgrade head` | `data/investor.duckdb` exists; 3 tables present |
| 2 | `uv run python scripts/load_targets.py` | `target_allocation` has rows for every ticker in `targets.yaml`, all with `effective_to IS NULL` |
| 3 | `uv run python scripts/sync_positions.py` | Stdout: `wrote N positions snapshots + 1 broker_account row` |
| 4 | `uv run python scripts/show_gap.py` | Pretty table with a row per target ticker |
| 5 | `uv run uvicorn src.investor.main:app --port 8000` | Server boots; logs show APScheduler starting |
| 6 | `curl localhost:8000/health` | 200, JSON includes a non-null `last_sync_ts` |
| 7 | `curl localhost:8000/gap \| jq` | Same data as the CLI table |
| 8 | `curl -X POST localhost:8000/admin/run-sync` | New row appended to `positions_snapshot` |
| 9 | `docker compose build && docker compose up -d` | Container reaches `healthy` state within 60 s |
| 10 | `curl localhost:8000/health` (Docker) | Same payload as Mac-native |
| 11 | `docker compose down && docker compose up -d` | Data persists; new `last_sync_ts` after restart |
| 12 | Repeat steps 5–7 next day | All still green; positions reflect any paper trades you made overnight |

Once all 12 pass, **commit and tag**:

```bash
git add -A
git commit -m "phase 0: foundation complete"
git tag v0.0.1-phase-0
git push --tags
```

You now have a working spine. Phase 1 (recurring schedule + email + drift bands + multi-day history) is mostly additive — you won't be refactoring this scaffolding.

---

## 13. Common Phase 0 pitfalls

1. **DuckDB single-writer.** If you have `uvicorn --reload` running and try to run `scripts/sync_positions.py` simultaneously, you'll get a lock error. Stop the server, run the script, restart. Phase 1 will move all writes inside the FastAPI process to avoid this.
2. **Alpaca paper account empty.** With zero positions, the gap query returns 100% gap on everything. Place a couple of paper trades in the Alpaca dashboard so the math is exercised against non-trivial data.
3. **Timezone confusion.** Alpaca timestamps are UTC; the scheduler runs in `America/New_York`; SQL aggregations should use the snapshot's UTC `ts`. Standardise on UTC at the storage layer; convert only at display.
4. **Cash buffer.** If your targets sum to 100 but you have 5% cash, every ticker will look slightly under-target on day one. The pragmatic fix: in the gap query, scale `current_pct` by `equity_usd / (equity_usd - cash_buffer_usd)` — but that's actually a Phase 1 decision; for Phase 0 just accept the small distortion and move on.
5. **Forgetting the read-only `config/` mount.** If you edit `targets.yaml` on the host, you want it picked up without rebuilding the image. The `:ro` mount handles this; just make sure you've reloaded the targets via `load_targets.py` (or restarted the container) so the DB matches the YAML.

---

## 14. ADR-0001 — Rebalance bands *(superseded: shipped as ADR-0008)*

> **Outcome.** This was written up as `docs/adr/0008-rebalance-bands.md`, not 0001 —
> the number 0001 ended up meaning the broker-adapter decision in every later
> citation. The choice made was **absolute per-ticker bands**.

Before Phase 1 starts, write an ADR with one decision: *absolute bands* (e.g., target ±5 percentage points) **or** *relative bands* (e.g., target ±25% of target). Pick one, write three sentences explaining why. The `band_low_pct` / `band_high_pct` columns already exist; you're just locking the formula that populates them.

This is the only ADR Phase 0 demands. Defer the rest until they actually block work.

---

*When all 12 smoke-test rows are green and the v0.0.1-phase-0 tag is pushed, Phase 0 is done.*
