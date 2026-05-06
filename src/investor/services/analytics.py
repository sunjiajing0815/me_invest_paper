"""DuckDB connection factory for bar analytics.

Creates a `price_bar` view that normalises the Parquet column names
(symbol → ticker, timestamp → date) so all downstream SQL works uniformly.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import duckdb


@contextmanager
def duckdb_conn(bars_dir: str = "data/bars") -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """Yield an in-memory DuckDB connection with a `price_bar` view over the Parquet files.

    The view maps Alpaca's column names to the canonical names used in all analytics queries:
      symbol    → ticker
      timestamp → date  (cast to DATE)
    """
    con = duckdb.connect()
    # Check files exist first — read_parquet raises IOException on an empty glob.
    import glob as _glob
    has_files = bool(_glob.glob(f"{bars_dir}/*.parquet"))
    if has_files:
        con.execute(f"""
            CREATE OR REPLACE VIEW price_bar AS
            SELECT
                symbol                   AS ticker,
                CAST(timestamp AS DATE)  AS date,
                open, high, low, close,
                volume, trade_count, vwap
            FROM read_parquet('{bars_dir}/*.parquet')
        """)
    else:
        # Empty view with the expected schema so callers can safely query and get 0 rows.
        con.execute("""
            CREATE OR REPLACE VIEW price_bar AS
            SELECT
                NULL::VARCHAR    AS ticker,
                NULL::DATE       AS date,
                NULL::DOUBLE     AS open,
                NULL::DOUBLE     AS high,
                NULL::DOUBLE     AS low,
                NULL::DOUBLE     AS close,
                NULL::BIGINT     AS volume,
                NULL::BIGINT     AS trade_count,
                NULL::DOUBLE     AS vwap
            WHERE FALSE
        """)
    try:
        yield con
    finally:
        con.close()
