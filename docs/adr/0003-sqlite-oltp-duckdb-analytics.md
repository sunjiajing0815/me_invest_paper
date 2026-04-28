# ADR-0003: SQLite for OLTP, DuckDB for Analytics

**Date:** 2026-04-28
**Status:** Accepted

## Context

Phase 0 used DuckDB for everything — both transactional inserts/updates and analytical queries. This was initially convenient (one file, one engine) but created ongoing friction:

- Alembic required a hand-written `DuckDBImpl` stub (ADR-0002) because `duckdb-engine` ships no DDL implementation.
- `--autogenerate` was unusable (DuckDB doesn't implement `pg_catalog.pg_collation`).
- `pool_size=1` was required to avoid DuckDB's single-writer file lock.
- None of the Phase 0 tables benefit from DuckDB's vectorized engine — they contain tens to hundreds of rows.

## Decision

Split the storage layer:

| Workload | Engine | Path |
|---|---|---|
| OLTP (ORM reads/writes) | SQLite via SQLAlchemy | `data/investor.db` |
| Analytics (Phase 1+) | DuckDB directly (`import duckdb`) | `data/bars/*.parquet` |

SQLite is built into Python's stdlib, has full native Alembic support (including `--autogenerate`), and supports all window functions used today (`ROW_NUMBER() OVER`) since version 3.25 (2018).

DuckDB is retained as a direct Python dependency (not via SQLAlchemy / `duckdb-engine`) for Phase 1+ analytical workloads that genuinely benefit from it.

## Why SQLite for OLTP

- Zero extra dependency (`sqlite3` is in stdlib; `duckdb-engine` removed)
- Native Alembic dialect: `SQLiteImpl` is built-in; no stub required
- `--autogenerate` works
- `render_as_batch=True` in `migrations/env.py` handles column renames/drops transparently
- `check_same_thread=False` replaces the `pool_size=1` workaround — APScheduler and FastAPI can share the connection without risk

## Why keep DuckDB for analytics

Phase 1+ introduces Parquet-based daily OHLCV bars (`data/bars/*.parquet`). Analytical queries over years of daily data — moving averages, support/resistance scans, backtesting — are exactly the workload DuckDB's vectorized engine is designed for:

```python
import duckdb
conn = duckdb.connect()
conn.execute("SELECT ticker, AVG(close) OVER (...) FROM read_parquet('data/bars/*.parquet')")
```

SQLite has no native Parquet support. DuckDB's `read_parquet()` table function eliminates the need for a separate ETL pipeline.

## How DuckDB will be used from Phase 1

- Direct `import duckdb` in analytical service functions
- **Not** via SQLAlchemy or `duckdb-engine`
- Reads Parquet files; does not write to any shared DB file
- Can join Parquet data with SQLite tables via DuckDB's SQLite scanner when needed:
  ```python
  conn.execute("ATTACH 'data/investor.db' AS sq (TYPE SQLITE)")
  ```

## Consequences

- `duckdb-engine` removed from `pyproject.toml`
- `duckdb` kept as a runtime dependency
- `DUCKDB_PATH` env var replaced by `SQLITE_PATH`
- `data/investor.duckdb` archived; `data/investor.db` is the active database
- Alembic revision `0047fb7675f2` marks the SQLite switchover point
- Phase 5 path unchanged: SQLite → Postgres when multi-user scale requires it
