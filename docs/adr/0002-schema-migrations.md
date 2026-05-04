# ADR-0002: Three-Tier Storage Architecture

**Date:** 2026-04-28 (retroactive — decision taken during Phase 0/1 transition)
**Status:** Accepted

## Context

The system needs to store three distinct kinds of data with very different access patterns:

1. **Transactional state** — target allocations, position snapshots, broker account values, metadata. Rows in the hundreds to low thousands. Access pattern: key lookups, small window-function queries, idempotent upserts. Requires schema migrations, concurrent read safety, and Python library support.

2. **Analytical time-series** — daily OHLCV bars per ticker, potentially years of history. Access pattern: full-column scans, rolling aggregations, cross-ticker comparisons. Requires vectorized execution; row-oriented storage is a bottleneck.

3. **Cold bar archive** — immutable daily bars written once by a backfill script, appended daily thereafter. Must survive DB schema migrations without reprocessing. Must be readable by the analytical engine without ETL.

Using a single engine for all three would mean either accepting poor analytical performance (SQLite row-store) or fighting Alembic support, single-writer locks, and OLTP hazards (DuckDB).

## Decision

Three-tier storage, one engine per tier:

| Tier | Engine | Location | Access pattern |
|---|---|---|---|
| OLTP | SQLite via SQLAlchemy + Alembic | `data/investor.db` | ORM reads/writes, session-scoped |
| Analytics | DuckDB (direct `import duckdb`) | in-memory, reads from Parquet | vectorized scans, window functions |
| Cold bars | Parquet files | `data/bars/<TICKER>.parquet` | written by scripts, read by DuckDB |

## Why SQLite for OLTP

- **Zero extra dependency** — `sqlite3` is in Python's stdlib; no engine package required
- **Native Alembic dialect** — `SQLiteImpl` is built-in; `--autogenerate` works; `render_as_batch=True` handles column renames and drops transparently (see ADR-0003)
- **Concurrent reads** — `check_same_thread=False` lets APScheduler's background thread and FastAPI's request threads share one engine without risk
- **Window functions** — `ROW_NUMBER() OVER (PARTITION BY ...)` supported since SQLite 3.25 (2018)
- **Phase 5 path** — SQLite → Postgres is a well-trodden migration; DuckDB is not a drop-in OLTP replacement

## Why DuckDB for analytics

- **Vectorized columnar execution** — moving averages, support/resistance scans, and cross-ticker aggregations over years of daily bars run in milliseconds
- **`read_parquet()` table function** — queries Parquet files directly, no ETL pipeline needed:
  ```python
  import duckdb
  conn = duckdb.connect()
  conn.execute("""
      SELECT symbol, AVG(close) OVER (PARTITION BY symbol ORDER BY timestamp ROWS 19 PRECEDING)
      FROM read_parquet('data/bars/*.parquet')
  """)
  ```
- **SQLite scanner** — DuckDB can attach and join against the OLTP database in one query:
  ```python
  conn.execute("ATTACH 'data/investor.db' AS sq (TYPE SQLITE)")
  conn.execute("""
      SELECT b.symbol, b.close, t.target_pct
      FROM read_parquet('data/bars/*.parquet') b
      JOIN sq.target_allocation t ON b.symbol = t.ticker
      WHERE t.effective_to IS NULL
  """)
  ```
- **No file-lock hazard** — used in-memory only; Parquet files are immutable once written, so no single-writer constraint
- **Used directly, not via SQLAlchemy** — `import duckdb` in analytical service functions; never via `duckdb-engine`

## Why Parquet for cold bar storage

- **Schema-independent** — Parquet carries its own schema; SQLite migrations never touch bar files
- **Columnar format** — DuckDB reads only the columns a query touches, not full rows
- **Per-ticker files** — `data/bars/VOO.parquet` etc.; adding a ticker means adding one file; backfilling one ticker doesn't touch others
- **Append pattern** — `update_bars.py` deduplicates by timestamp and rewrites; no transactional write concerns

## How the tiers interact

```
FastAPI / APScheduler
    │
    ├── session_scope() ──► SQLite (investor.db)
    │       ORM reads/writes via SQLAlchemy
    │
    └── duckdb.connect() ──► Parquet files (data/bars/*.parquet)
            analytical queries via read_parquet()
            optionally: ATTACH 'investor.db' AS sq (TYPE SQLITE)
```

Services in `src/investor/services/` that touch OLTP receive a `Session` argument. Services that touch analytics create their own `duckdb.connect()`. These two never share a connection object.

## Consequences

- `duckdb-engine` is not a dependency (removed during Phase 0 carryover; see ADR-0003 for the migration history)
- `duckdb` is a runtime dependency, used directly
- `pyarrow` is a runtime dependency, used by pandas for Parquet I/O in backfill/update scripts
- All SQLite schema changes go through Alembic (see ADR-0003)
- Phase 5 (multi-user): SQLite → Postgres for OLTP; DuckDB/MotherDuck for analytics; Parquet layer unchanged
