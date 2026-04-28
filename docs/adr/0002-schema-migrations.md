# ADR-0002: Schema Migrations with Alembic + DuckDB

**Date:** 2026-04-28
**Status:** Accepted

## Context

Phase 0 used four hand-rolled `ALTER TABLE … ADD COLUMN IF NOT EXISTS` statements in `db.py::_migrate_broker_account_columns()`. This approach breaks on column renames, type changes, and rollbacks — all of which are needed in Phases 2–4. A proper migration tool is required before adding more schema changes.

## Decision

Adopt Alembic. Use it as a version tracker with manually written DDL in migration files. Never use `--autogenerate`.

## DuckDB-specific constraints

### 1. Missing Alembic DDLImpl

`duckdb-engine` does not register an Alembic dialect implementation. Without a stub, every `alembic` command fails:

```
KeyError: 'duckdb'
```

**Fix:** Register a minimal stub in `migrations/env.py`:

```python
from alembic.ddl.impl import DefaultImpl

class DuckDBImpl(DefaultImpl):
    __dialect__ = "duckdb"
```

### 2. `--autogenerate` is not viable

Alembic's autogenerate introspects `pg_catalog.pg_collation` to detect collation changes. DuckDB does not implement this PostgreSQL catalog table:

```
CatalogException: Table with name pg_collation does not exist!
```

**Decision:** Write all migration files by hand. The baseline revision (`6c9b40ddd25c`) is a no-op that stamps the existing Phase 0 schema. Future revisions use `op.create_table`, `op.add_column`, etc., written manually.

### 3. `batch_alter_table` for column renames and type changes

DuckDB does not support `ALTER TABLE … ALTER COLUMN TYPE` for some type changes. Use Alembic's batch mode when needed:

```python
with op.batch_alter_table("my_table") as batch_op:
    batch_op.alter_column("old_name", new_column_name="new_name", ...)
```

### 4. `pool.NullPool` in `env.py`

`run_migrations_online()` creates a separate engine with `poolclass=pool.NullPool`. This avoids sharing a connection with the app engine, which is unreliable with DuckDB's single-writer constraint.

## Baseline strategy

`Base.metadata.create_all(checkfirst=True)` runs first in `init_db()` to handle fresh database creation. Alembic `upgrade head` runs immediately after to apply any pending incremental migrations. The two coexist because `create_all` is idempotent and the baseline revision is a no-op (empty `upgrade()`/`downgrade()`).

## Consequences

- All schema changes from this point forward are tracked as Alembic revisions.
- `--autogenerate` is permanently off-limits for this project.
- The `meta` table (added in revision `71b1bd302b7e`) stores YAML content hashes for idempotent target loading.
- Column renames and type changes require `batch_alter_table`.
