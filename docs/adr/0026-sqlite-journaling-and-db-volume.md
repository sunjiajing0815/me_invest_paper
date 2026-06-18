# ADR-0026 — SQLite journaling mode and database storage location

**Status:** Accepted
**Date:** 2026-06-18

## Context

A target reload for the Moomoo account (adding TSLA, fixing the QQQ band) was run,
returned `updated`, and was verified present in the database — yet days later the
database had silently reverted to the pre-reload state: the new rows were gone with
**no trace** (the superseded rows were never even closed), while position snapshots had
continued to advance normally.

Root-cause investigation found:

1. The database was in **WAL journal mode** (`PRAGMA journal_mode=wal`), even though no
   application code sets it. WAL is a persistent on-disk property; it was almost certainly
   turned on historically by langgraph's `SqliteSaver` (Phase 3b, which runs
   `PRAGMA journal_mode=WAL`). The code later moved to `MemorySaver` (CLAUDE.md gotcha #12)
   and dropped the SqliteSaver tables, but the WAL mode was never reverted and there was no
   checkpoint management.
2. The SQLite file lived on a **macOS Docker Desktop bind mount** (`./data:/app/data`).
3. **WAL is unsafe there.** WAL relies on a memory-mapped shared-memory file (`-shm`) and
   POSIX file locking that Docker Desktop's bind-mount virtualisation (VirtioFS/gRPC-FUSE)
   does not reliably provide — SQLite's own docs state WAL "does not work on a network
   filesystem." Committed transactions accumulated in the `-wal` file (observed: the main
   `.db` lagged the `-wal` by ~15 hours) and the un-checkpointed tail was lost across a
   checkpoint/restart that the incoherent mount mishandled, reverting the DB to its last
   checkpointed state.

This is a storage-layer data-loss bug, independent of any application logic.

## Decisions

### 1. Disable WAL — use the DELETE rollback journal with `synchronous=FULL`

`db.py` attaches a SQLAlchemy `connect` event listener (before any connection is opened)
that runs `PRAGMA journal_mode=DELETE` and `PRAGMA synchronous=FULL` on every connection.
The rollback journal needs no `-shm`/mmap and is safe on any filesystem; `synchronous=FULL`
maximises durability. The app is single-writer (CLAUDE.md convention #7), so WAL's
concurrent-reader benefit is irrelevant. `init_db` additionally **fails fast** if the DB is
still in WAL mode after configuration.

### 2. Move the SQLite DB off the bind mount onto a Docker named volume

The OLTP database now lives on a named volume (`dbdata:/app/db`, `SQLITE_PATH=
/app/db/investor.db`) — real ext4 inside the Docker VM, where SQLite locking/journaling
behave correctly — instead of the `./data` bind mount. Parquet bars and the DuckDB file
stay on the `./data` bind mount: they are append-only / single-file artifacts with no
journaling, and benefit from host-side visibility.

Belt-and-suspenders: even though either fix alone closes the hole, we apply both — DELETE
journaling protects any future deployment that points `SQLITE_PATH` back at a bind mount,
and the named volume removes the unreliable mount from the OLTP path entirely.

## Consequences

- No more silent rollbacks: writes are durably flushed to the main `.db` on commit.
- The database is no longer directly visible at `./data/investor.db` on the host; inspect it
  via `docker compose exec app ...` or `docker run` against the `dbdata` volume. Back it up
  with `docker run --rm -v me_invest_dbdata:/db -v "$PWD":/out alpine cp /db/investor.db /out/`.
- One-time migration: the live DB was checkpointed (`wal_checkpoint(TRUNCATE)`) and converted
  to DELETE mode, then copied into the named volume before cutover.
- Local (non-Docker) runs still use `./data/investor.db` from `.env`; the DELETE-mode pragma
  keeps that safe too.
