# ADR-0003: Schema Migrations with Alembic + SQLite

**Date:** 2026-04-28 (retroactive — decision taken during Phase 0 carryover)
**Status:** Accepted

## Context

Phase 0 initially used four hand-rolled `ALTER TABLE … ADD COLUMN IF NOT EXISTS` statements inside `db.py::_migrate_broker_account_columns()`. That approach breaks on column renames, type changes, dropped tables, and rollbacks — all needed in Phases 2–4. A proper migration tool was required.

An earlier iteration of this codebase used DuckDB for OLTP, which made Alembic painful: `duckdb-engine` shipped no `DDLImpl`, autogenerate failed against DuckDB's missing `pg_catalog.pg_collation`, and migrations had to be written with a hand-rolled stub. When the storage split (ADR-0002) moved OLTP to SQLite, all those DuckDB-specific constraints disappeared.

## Decision

Adopt Alembic with the native SQLite dialect. Write migration content by hand; use `--autogenerate` only for schema inspection, not as the source of truth.

## SQLite dialect — what works out of the box

| Feature | DuckDB (old) | SQLite (current) |
|---|---|---|
| Alembic `DDLImpl` | required a hand-written stub | built-in `SQLiteImpl` |
| `--autogenerate` | failed (`pg_collation` missing) | works |
| `render_as_batch` | needed for column changes | needed for column changes |
| `pool.NullPool` in env.py | required (single-writer lock) | not needed |

## `render_as_batch=True`

SQLite cannot `ALTER COLUMN` or `DROP COLUMN` directly. Alembic's batch mode works around this by:

1. Creating a new table with the desired schema
2. Copying all data
3. Dropping the old table
4. Renaming the new table

This is set once in `migrations/env.py` and applies to all future revisions:

```python
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    render_as_batch=True,   # required for SQLite column changes
)
```

Column renames and type changes use the batch context:

```python
with op.batch_alter_table("broker_account") as batch_op:
    batch_op.alter_column("old_name", new_column_name="new_name")
```

## Baseline strategy

On every app startup, `init_db()` runs two operations in sequence:

```python
Base.metadata.create_all(_engine, checkfirst=True)   # idempotent: creates tables if missing
alembic_command.upgrade(alembic_cfg, "head")          # applies any pending migrations
```

The two coexist safely: `create_all` is a no-op when tables already exist; the baseline revision (`0047fb7675f2`, the SQLite switchover marker) has an empty `upgrade()` so it also becomes a no-op on established databases.

## Revision history

| Revision | Description |
|---|---|
| `6c9b40ddd25c` | Phase 0 baseline — stamps the existing schema, no DDL |
| `71b1bd302b7e` | Add `meta` table (stores YAML content hashes for idempotent target loading) |
| `0047fb7675f2` | SQLite switchover marker — no-op DDL, stamps the migration point |

## Policy: manual migration content, autogenerate for inspection only

`--autogenerate` is available and correct with SQLite, but migration files must be reviewed before committing — autogenerate occasionally emits spurious column-type changes due to SQLite's loose affinity rules. The workflow:

```bash
# Inspect what autogenerate would do (review before using):
uv run alembic revision --autogenerate -m "description"

# Apply pending migrations:
uv run alembic upgrade head

# Roll back one step:
uv run alembic downgrade -1
```

## Consequences

- All schema changes from Phase 0 onward are tracked as Alembic revisions
- `render_as_batch=True` is permanent — never remove it; SQLite will always need it
- The `meta` table (revision `71b1bd302b7e`) is used for YAML content hashes; new key/value pairs are added by the app, not by migrations
- Phase 5 Postgres migration: swap `sqlite:///` for `postgresql://` in `SQLITE_PATH` (rename env var), generate a fresh Alembic revision — the revision history remains intact as documentation
