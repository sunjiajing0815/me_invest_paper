# Phase 4.9a — Multi-Broker Plumbing + Per-Broker Reports — Progress Report

> **Status: CODE-COMPLETE — Stages A, B, and C committed on `main`.**
> The app is fully multi-broker across the data model, read path, write path, jobs,
> scheduler, and API. The only thing between here and the `v0.4.9a.0` tag is the
> 2-broker smoke test (connect Moomoo paper alongside Alpaca; confirm two daily +
> two weekly emails, per-broker audit columns, per-broker mode isolation).
>
> **Commits (on top of `v0.4.8`):** foundation hardening → B1 (auto_trade_state) →
> B2 (adapter factory) → B3–B5 (partition-key threading) → B6 (per-broker targets) →
> B7+B-API+B8 (emails + endpoint params + scheduler fan-out) → B9 (NOT NULL) →
> C (onboarding endpoints) → docs (ADR-0024, CLAUDE.md, README, product_plan).

## Context

The app was hard-wired to one broker: `make_adapter(settings)` returned a single adapter, `app.state.adapter` was singular, every per-account table was implicitly Jane's one account, and `auto_trade_mode` was a single `meta` key. Phase 4.9a makes a single user hold positions across multiple broker accounts simultaneously, with separate daily/weekly emails per broker. Suggest-only holds across all brokers; auto-trade LIVE stays Alpaca-only.

**Approved scope = Foundation + Moomoo** (per the planning Q&A): deliver the schema migration + multi-broker fan-out, validated using the *existing* `MoomooAdapter` as broker #2. **IBKR and Tiger adapters are out of scope** for 4.9a (each needs host-side dependencies and its own soak ladder); ADR-0025/0026 are deferred with them.

**Approved account model = dual-purpose `broker_account` table, Integer PKs** (no new identity table, no UUIDs). Because `broker_account` uses close-and-insert (its auto-increment `id` changes on every cash/equity change), a stable **`account_ref`** column is the partition key; per-account tables carry `broker_account_id = account_ref`. Identity columns (`nickname`, `is_active`, `connection_config`) ride on the latest open row.

---

## Stage A — Multi-broker schema migration ✅ (committed)

### What landed

**`models.py`:**
- `broker_account`: added `account_ref` (stable partition key), `nickname`, `is_active` (soft-delete, default True), `connection_config` (JSON text). Now documented as dual-purpose identity + time-versioned state.
- `broker_account_id` partition column (plain integer, no DB FK — app-enforced per the `OrderExecution.suggestion_id` convention) on `target_allocation`, `positions_snapshot`, `order_suggestion`, `order_execution`.
- New `AutoTradeState` model: per-broker `mode` + optional cap overrides; replaces `meta.auto_trade_mode`.
- `order_suggestion` unique constraint → `(broker_account_id, week_of, ticker, side)`.

**Migration `d8589fe198cf`:**
- Adds the columns; backfills every existing per-account row to Jane's canonical Alpaca account (latest open `broker_account` row); sets identity columns; seeds `auto_trade_state` from the old `meta.auto_trade_mode`; deletes the meta key; swaps the unique constraint; adds indexes. `auto_trade_state` creation is guarded (`create_all` may make it first in `init_db`). Downgrade reverses and restores the meta key.

**Validation against a copy of the real DB** (`data/investor.db`, never the live file — copied via SQLite backup API):
- 31 / 309 / 22 / 30 rows in the four per-account tables → exactly **one** `broker_account_id` group (= 59, Jane's latest open row), counts preserved, zero NULLs.
- `auto_trade_state` seeded `(59, 'LIVE')` — prior mode preserved; `meta.auto_trade_mode` removed.
- Identity columns set on the latest open row (`nickname='Alpaca paper'`, `is_active=1`, `connection_config` naming the env vars).
- Full **downgrade → re-upgrade round-trip** clean.

**`tests/test_migration_phase4_9a.py`** (2 tests): builds a representative pre-4.9a DB (alembic to parent + raw-SQL for the create_all-only tables), runs the migration, asserts the backfill invariants and the downgrade round-trip. CI-safe (no dependency on the real DB).

### Two pre-existing latent bugs fixed as prerequisites

1. **`f2680` migration crash on fresh DB.** `op.drop_table('checkpoints')` / `drop_table('writes')` were unconditional — langgraph SqliteSaver tables that leaked into the migration via autogenerate (the app uses `MemorySaver`, CLAUDE.md gotcha #12). On any DB without those tables, `alembic upgrade head` crashed. Fixed to `DROP TABLE IF EXISTS` (no-op on Jane's already-migrated DB; the revision won't re-run there).
2. **`env.py` ignored the programmatic DB URL.** `get_url()` always used `SQLITE_PATH`, ignoring `config.set_main_option("sqlalchemy.url", …)`. The migration test initially ran against the real DB path as a result (caught immediately — harmless no-op). Fixed to honor a real `sqlite:///` config URL, falling back to `SQLITE_PATH` for the `alembic.ini` placeholder.

### Structure issue — FIXED (foundation hardening)

`broker_account`, `target_allocation`, and `positions_snapshot` were created by SQLAlchemy `create_all`, **not** by any Alembic migration, and `init_db()` ran `create_all` *then* alembic — so on a fresh build the two collided (`create_all` builds every current-model table, then the `create_table` migrations and `d8589`'s column-adds hit "already exists"). The repo only worked because the existing DB was already at head.

Resolved by making **Alembic the single source of truth**:
- New migration `7d25844a8a9a_adopt_legacy_create_all_tables` creates the three former create_all-only tables in their pre-4.9a shape, inspector-guarded (a no-op on the real DB), chained `62b0733b198f → 7d25844a8a9a → d8589`.
- `init_db()` reordered to run `alembic upgrade head` **first**, then `create_all(checkfirst=True)` as a backstop only.
- Regression test `tests/test_fresh_schema.py` asserts a fresh `alembic upgrade head` yields **every** `Base.metadata` table (verified: 15/15, none missing). A fresh `init_db` deploy now builds the complete schema with no collision.

This foundation hardening landed as its own commit, separate from (and before) the gated 4.9a schema migration — see "Files changed" below.

### Restart safety (gate cleared)

The migration deletes `meta.auto_trade_mode` and seeds `auto_trade_state`. **B1 rewired `_get_mode`/`set_mode`/promote/kill-switch to `auto_trade_state` before any restart**, so applying the migration on a restart reads the prior mode back per broker — auto-trade does **not** silently fall to OFF. Restarting now applies the full chain (`7d25 → d8589 → 6a4a`) and the app runs fully multi-broker; it's a clean restart point.

---

## Stages B & C — completed

### Stage B — Multi-broker fan-out ✅
- **B1**: `auto_trade_state` mode read/write (cleared the restart gate); guards/caps/kill-switch scoped per `broker_account_id`.
- **B2**: `make_account_adapter` / `build_account_adapters`; `app.state.adapters` in the lifespan.
- **B3–B5**: 5 SQL files parameterized; `gap`/`snapshot`/`compose_daily_report` + the 6 readers scoped; per-broker job loops; reconciliation tags + scopes executions; `persist_suggestions` + the review graph threaded; `services/accounts.py` added.
- **B6**: per-broker `data/targets/<account_ref>.yaml` (primary falls back to `config/targets.yaml`); `load_targets_into_db` scoped (per-account hash key + expiry); reload loop.
- **B7**: per-broker email nickname/broker line + `[nickname]` subjects.
- **B8**: scheduler crons fan out over `app.state.adapters` (`*_all_brokers` loops; added expiry + auto-trade loops).
- **B-API**: every account-scoped endpoint takes `?broker_account_id` (reads → primary default; job triggers/bulk mutations → all-active default; 404 on invalid). `/health` lists all active accounts.
- **B9**: `broker_account_id` (4 tables) + `account_ref` flipped `NOT NULL` (migration `6a4a9fada1dc`); model updated.

### Stage C — Onboarding + docs + ADR ✅
- `POST/GET/DELETE /admin/broker-accounts` — onboard (fresh `account_ref`, seed `auto_trade_state` OFF, register adapter live), list, soft-delete.
- **ADR-0024** written; CLAUDE.md (mission, conventions #3 + #17, repo layout, env note), README, `product_plan.md` updated.

---

## Test summary

| Milestone | Tests |
|---|---|
| Phase 4.8 close (after flaky-test fix) | 343 |
| Phase 4.9a Stage A + foundation hardening | 346 |
| Stage B (B1 mode isolation, B2 factory, B3 gap isolation, B-API scope/404) | 360 |
| Stage C (broker-account onboarding) | **364** |

`uv run pytest` → 364 passed, 1 skipped. `ruff check src/ tests/` clean. `mypy src/` → 30 pre-existing errors, no new ones. The d8589 + 6a4a migrations were validated on a copy of the real DB (one `broker_account_id` group, counts preserved, all 5 columns NOT NULL, constraints survived the batch recreate, downgrade round-trips); fresh `alembic upgrade head` builds all 15 model tables.

## Files changed (Stage A)

The work is split into three commits so the safe foundation lands first, independently deployable, ahead of the gated 4.9a schema:

**Commit 1 — infra foundation (safe, no auto-trade gate):**
| File | Change |
|---|---|
| `migrations/env.py` | honor programmatic `sqlalchemy.url` (prereq fix) |
| `migrations/versions/f2680eed32f8_…py` | `DROP TABLE IF EXISTS` checkpoints/writes (prereq fix) |
| `migrations/versions/7d25844a8a9a_adopt_legacy_create_all_tables.py` | adopt the 3 create_all-only tables into Alembic (inspector-guarded) |
| `src/investor/db.py` | `init_db()` alembic-first, `create_all` backstop only |
| `tests/test_fresh_schema.py` | regression: fresh `alembic upgrade head` yields every model table |

**Commit 2 — 4.9a schema (gated — deletes `meta.auto_trade_mode`):**
| File | Change |
|---|---|
| `src/investor/models.py` | identity cols + `account_ref`, `broker_account_id` ×4, `AutoTradeState`, constraint swap |
| `migrations/versions/d8589fe198cf_phase4_9a_multi_broker.py` | new migration (backfill + auto_trade_state seed + constraint swap); chained after the adopt migration |
| `tests/test_migration_phase4_9a.py` | migration backfill + downgrade tests |

**Commit 3 — docs:** `README.md`, `plans/phase_4_9a_completion.md`.

## Tag

Stages A–C are code-complete. Apply `v0.4.9a.0` once the 2-broker smoke checklist is green:
1. Migration: existing single-broker rows carry through to one `broker_account_id` group (validated on a real-DB copy).
2. `POST /admin/broker-accounts` connects Moomoo paper; `make_account_adapter` returns the right adapter; it appears in `/health`.
3. Daily report fires per active broker → two emails with `[nickname]` subjects.
4. Weekly suggestions Sunday → each broker its own email; a ticker held in both yields independent per-broker suggestions.
5. Promote Alpaca → LIVE leaves Moomoo OFF in `auto_trade_state`.
6. Stale-live-order guard: two live AAPL orders across *different* brokers allowed; two on the *same* broker still blocked.
7. `data/targets/<id>.yaml` edited independently; `config/targets.yaml` still aliases the primary.
8. Soft-delete (`is_active=False`) excludes a broker from cron loops; its history stays queryable.
