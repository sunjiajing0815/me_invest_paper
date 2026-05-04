# ADR-0004: Bar Storage — Parquet Files with DuckDB read_parquet()

**Date:** 2026-04-28
**Status:** Accepted

## Context

Phase 1 introduces daily OHLCV (open/high/low/close/volume) bar data for each ticker in the watchlist. This data is used in Phase 2+ to compute moving averages, identify support/resistance levels, and drive rebalance suggestions.

Three storage options were considered:

| Option | Storage | Query engine |
|---|---|---|
| A | Parquet files (`data/bars/<TICKER>.parquet`) | DuckDB `read_parquet()` |
| B | SQLite table (`price_bar`) | SQLAlchemy / raw SQL |
| C | DuckDB table (`investor.duckdb`) | DuckDB SQL |

## Decision

**Option A** — Parquet files queried directly via DuckDB.

## Rationale

### Why not a SQLite table (Option B)

SQLite is used for OLTP (targets, positions, account). Its row-oriented storage is well-suited for hundreds of rows and key-value lookups. It is not suited for:

- Window functions over 100k+ rows (2 years × 6 tickers × 252 trading days ≈ 3k rows today, growing to 50k+ over 10 years with more tickers)
- Rolling averages, grouped aggregations, and cross-ticker comparisons that analytical queries require

DuckDB's vectorized columnar engine handles these workloads in milliseconds where SQLite would require full table scans.

### Why not a DuckDB database file (Option C)

ADR-0003 established that DuckDB's single-writer file lock and limited Alembic support make it unsuitable for OLTP. Using a shared DuckDB file for bars would reintroduce the same concurrency hazard. Parquet files are immutable once written — no locking required.

### Why Parquet + read_parquet() (Option A)

- Parquet is a columnar format: DuckDB reads only the columns a query touches, not the full row
- Files survive DB migrations — schema changes to SQLite do not affect the bar archive
- `duckdb.connect().execute("SELECT ... FROM read_parquet('data/bars/*.parquet')")` runs without any setup overhead
- Individual per-ticker files make the backfill, partial refresh, and ticker addition patterns trivial
- DuckDB's SQLite scanner lets us join Parquet bars with SQLite targets in a single query when needed:

```python
conn = duckdb.connect()
conn.execute("ATTACH 'data/investor.db' AS sq (TYPE SQLITE)")
conn.execute("""
    SELECT b.symbol, b.close, t.target_pct
    FROM read_parquet('data/bars/*.parquet') b
    JOIN sq.target_allocation t ON b.symbol = t.ticker
    WHERE t.effective_to IS NULL
""")
```

## Parquet file convention

- Path: `data/bars/<TICKER>.parquet` (uppercase ticker, e.g. `data/bars/VOO.parquet`)
- Schema: columns as returned by Alpaca's `StockBarsRequest` (`symbol`, `timestamp`, `open`, `high`, `low`, `close`, `volume`, `trade_count`, `vwap`)
- Written by `scripts/backfill_bars.py` (one-off bootstrap)
- Appended to daily by `scripts/update_bars.py` (or scheduler job in Phase 2)
- `data/bars/` is bind-mounted in Docker and gitignored

## Consequences

- `pyarrow` added as a runtime dependency (Parquet encoder/decoder for pandas)
- `duckdb` retained as a runtime dependency (direct `import duckdb`; not via SQLAlchemy)
- Phase 2 analytical services import `duckdb` directly — no SQLAlchemy session needed
- Phase 5 Postgres migration does not affect the Parquet layer
