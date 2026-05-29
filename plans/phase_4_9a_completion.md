# Phase 4.9a — Multi-Broker Plumbing + Per-Broker Reports — Progress Report

> **Status: IN PROGRESS — Stage A complete, Stages B & C pending.**
> This is *not* a phase-done report. Stage A (the multi-broker schema foundation)
> is committed and validated; the phase's Definition of Done (a second broker
> connected, two daily + two weekly emails) is **not yet met**. The app still
> operates single-broker until Stage B wires the per-broker fan-out.

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

### ⚠️ Deployment gate (must read before any restart)

The Stage A migration **deletes `meta.auto_trade_mode`** and moves the mode to `auto_trade_state`, but `services/auto_trade.py::_get_mode()` still reads `meta` — wiring it to `auto_trade_state` is **Stage B** work. Because `init_db()` runs `alembic upgrade head` on startup:

> **Restarting / redeploying the app before Stage B lands will apply the migration and silently fall auto-trade back to `OFF`** (currently `LIVE` → `OFF`).

This is the safe, suggest-only direction (no wrong orders — it just stops placing), but it is unintended. Jane's real DB is currently still at the parent revision `62b0733b198f` (migration not yet applied). **Do not restart in production until Stage B reads `auto_trade_state`.**

---

## Remaining work

### Stage B — Multi-broker fan-out (pending)
- `make_adapter(broker, connection_config)` factory + `app.state.adapters: dict[account_ref, BrokerAdapter]` in the lifespan.
- Update the 6 `BrokerAccount` "latest open row" readers (snapshot, daily_report, weekly_suggestions, weekly_review, suggestion_review, main) to filter by `account_ref`; `snapshot.py` writes `broker_account_id` and carries identity columns forward on close-and-insert.
- Per-broker job loops (daily/weekly/reconciliation/expiry) with `[nickname]` subject prefixes and `try/except … continue` isolation.
- Thread `broker_account_id` through `suggest` / `gap` / reconciliation / the auto-trade guards (`_get_mode` per account; caps/idempotency/wash-sale/stale-live-order scoped per broker) and the review graph (`weekly_market_context` stays user-level — one synthesis serves all brokers).
- Per-broker targets (`data/targets/<broker_account_id>.yaml`); `config/targets.yaml` aliases the primary for one release.
- Templates: nickname/broker line. `auto_trade_state` wiring: `_get_mode`, the promote endpoint's `broker_account_id`, scheduler entrypoints.

### Stage C — Onboarding + docs + ADR (pending)
- `POST /admin/broker-accounts` to create an identity row + seed `auto_trade_state` at `OFF` (how Moomoo gets connected as broker #2 for the smoke test).
- **ADR-0024** — multi-broker single-user data model (dual-purpose `broker_account` + `account_ref`; per-account `broker_account_id` no-FK; per-broker `auto_trade_state` + per-broker soak ladder; cross-broker wash-sale deliberately per-broker in 4.9a; news/levels/market-context stay user-level).
- Docs drift: CLAUDE.md (mission, convention #3 → `(broker_account_id, ticker)`, convention #11 → `auto_trade_state` per broker), README (further updates), `product_plan.md` 4.9a entry.

### Final tightening (pending)
- Flip `broker_account_id` model + DB to `NOT NULL` (follow-up migration) once every writer sets it.

---

## Test summary

| Milestone | Tests |
|---|---|
| Phase 4.8 close (after flaky-test fix) | 343 |
| Phase 4.9a Stage A (+2 migration tests) | 345 |
| Foundation hardening (+1 fresh-schema test) | **346** |

`uv run pytest` → 346 passed, 1 skipped. `ruff check src/ tests/` clean. `mypy src/` → 30 pre-existing errors, no new ones. Migration validated on a copy of the real DB (above); fresh `alembic upgrade head` builds all 15 model tables.

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

`v0.4.9a.0` is **not** appropriate yet — it should be applied only when the full phase (Stage A + B + C) is code-complete and the 2-broker smoke checklist is green.
