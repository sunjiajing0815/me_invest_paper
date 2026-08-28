# Phase 4.9a — Multi-Broker Plumbing + Per-Broker Reports — Progress Report

> **Status: SMOKE-TESTED + HARDENED — Alpaca + Moomoo live on `main`.**
> The app is fully multi-broker across the data model, read path, write path, jobs,
> scheduler, and API. The 2-broker smoke test ran live on **2026-05-31** (Moomoo on its
> REAL funded account alongside Alpaca, app in Docker); then **operating** it through real
> daily / weekly / movers / auto-trade cycles on **2026-06-01 → 06-02** surfaced a further batch of
> bugs — all fixed with regression tests (see **"Smoke test execution"** and
> **"Post-smoke-test fixes (live operation)"** below). Remaining before the `v0.4.9a.0`
> tag: a clean week of scheduled runs with no new surprises.
>
> **Commits (on top of `v0.4.8`):** foundation hardening → B1 (auto_trade_state) →
> B2 (adapter factory) → B3–B5 (partition-key threading) → B6 (per-broker targets) →
> B7+B-API+B8 (emails + endpoint params + scheduler fan-out) → B9 (NOT NULL) →
> C (onboarding endpoints) → docs (ADR-0024, CLAUDE.md, README, product_plan) →
> `47d5d2b` (docker extra_hosts) → `ee77737` (sync routing + stable primary) →
> `04d4fae` (OpenD RSA encryption) → **post-smoke-test hardening**: `b5d8103` `e60f434`
> `ba8742f` `391d062` `c87eb42` `86284c1` `98abe6b` `9413d41` `0047d53` `567ffb3`
> `e9bd724` `5f8cf92` `1b77dff`.

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
- 31 / 309 / 22 / 30 rows in the four per-account tables → exactly **one** `broker_account_id` group, counts preserved, zero NULLs. The group value is `jane_ref` = the **`id` of the latest-open `broker_account` row at migration time** (`59` on this particular copy), and the migration sets `account_ref` **and** every per-account `broker_account_id` to that same value in one transaction, so `broker_account_id == account_ref` by construction. The number is therefore non-deterministic (it's "the origin row's id") and differs between the copy and the live DB — see the note below; what's invariant is the equality, never the literal.
- `auto_trade_state` seeded `(jane_ref, 'LIVE')` — prior mode preserved; `meta.auto_trade_mode` removed.

> **Note — the live value is `61`, not `59`.** This validation ran on an earlier copy whose origin/latest-open row was `id=59`. The live migration ran later, when the Alpaca origin row was `id=61`, so on the live DB `account_ref == broker_account_id == 61` (and Moomoo, onboarded after, self-assigned `account_ref=62`). Confirmed live: every per-account `broker_account_id ∈ {61, 62}` matches a live (`effective_to IS NULL`) `account_ref`, zero orphans — and the Alpaca account has since close-and-inserted from origin `id=61` to live `id=74` with `account_ref` unchanged at `61`, which is the whole point of keying per-account rows on `account_ref` rather than the mutable `id`.
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

## Smoke test execution + hardening (2026-05-31) ✅

The live 2-broker smoke test (Moomoo connected alongside Alpaca, app in Docker)
exercised the real fan-out and surfaced bugs the unit tests had not. All fixed,
re-tested, and verified end-to-end against the running deployment.

### Setup
- `docker-compose.yml`: added `extra_hosts: ["host.docker.internal:host-gateway"]` so
  the container reaches host-side OpenD on a Linux VPS too (no-op on macOS Docker
  Desktop). Commit `47d5d2b`.
- Moomoo onboarded via `POST /admin/broker-accounts` → **`account_ref=62`**
  (`security_firm=FUTUAU`), `auto_trade_state` seeded **OFF**. Alpaca is `account_ref=61`.
- Per-account targets authored at `data/targets/62.yaml` (11 tickers; sum 90 + 10% cash
  buffer = 100; bands corrected to bracket each target; watchlist set to the target set).

### Three multi-broker bugs found + fixed (commit `ee77737`)
1. **Sync routing (Bug A).** `/admin/run-sync` ignored `?broker_account_id` and
   `run_sync_job` always used the *primary* adapter + *primary* account — so syncing
   broker B pulled broker A's positions and wrote them under B. Fix: the endpoint now
   takes `?broker_account_id` (default all-active) and syncs each account through its
   own `app.state.adapters[ref]`; added `run_sync_for_account` / `run_sync_all_brokers`.
2. **Unstable primary (Bug B).** `resolve_primary_account_ref` (and
   `_resolve_scope(default="primary")`) returned the *most-recently-synced* account.
   Onboarding stamps `last_sync=now`, so "primary" silently flipped to the new broker —
   mis-routing the sync above and every no-arg default (reads, lifespan adapter,
   promote). Fix: primary = **lowest active `account_ref`** (stable across onboards).
3. **Per-account targets not loaded in jobs (Bug C).** The daily/weekly jobs loaded
   `settings.targets_path` (the primary file) for every account. Fix: they now call
   `targets_path_for_account(settings, ref, is_primary=…)`; a non-primary account
   without its own file is skipped with a warning.

Regression test `tests/test_multibroker_sync.py`: onboarding a more-recently-synced
broker keeps primary on the lowest ref; per-account sync uses that account's own
adapter (no cross-contamination).

### Encrypted OpenD support (commit `04d4fae`)
OpenD was configured to require an encrypted connection while the adapter connected in
the clear, so InitConnect (proto 1001) failed with `check sha error`. Fix:
`MoomooAdapter` now calls Futu `SysConfig.enable_proto_encrypt(True)` +
`set_init_rsa_file(path)` (process-global, before any context) when a key path is set.
- New setting `opend_rsa_key_path` (env `OPEND_RSA_KEY_PATH`); per-account override via
  `connection_config["rsa_key_path"]`. No key → unencrypted (default; Alpaca unaffected).
- The 1024-bit PKCS#1 key lives at `data/secrets/moomoo_opend_rsa.txt` (gitignored,
  bind-mounted); only the *path* is in env/`connection_config`, never the key itself
  (honours "no secrets in the DB"). Tests in `tests/test_moomoo.py`.

### Recovery of the mis-synced account
Before the fix, account 62 held Alpaca's positions (from Bugs A/B). Recovery: backed up
the DB (`data/investor.db.bak-fix-2026-05-31`), deleted 62's bogus `positions_snapshot`
rows, and re-synced through the corrected, encrypted path. The bogus `broker_account`
state row was superseded by close-and-insert; Alpaca (61) was never touched.

### Verified end state
- **61 = Alpaca paper** — auto-trade `LIVE`, the primary, 10 targets, holdings intact.
- **62 = Moomoo** — **REAL funded** account, encrypted OpenD, **auto-trade OFF**
  (suggest-only), 11 targets, **15 real holdings** synced. Switched from SIMULATE to
  REAL by setting `connection_config.paper=false` (durable; carried forward on sync).
- No-arg `/positions` resolves to Alpaca (primary stable); gap for 62 uses its own 11
  targets; `check sha error` gone; re-sync returns `{"62": 15}` with zero errors.

## Post-smoke-test fixes — live operation (2026-06-01 → 06-02)

Running the deployed app through real daily / weekly / movers / auto-trade cycles surfaced
a further batch of bugs, each fixed with a regression test. Grouped by area.

### Currency & multi-currency reporting
- **Moomoo totals were ~5.6× too high** (`b5d8103`). `get_account` called Futu's
  `accinfo_query` with no `currency`, which defaults to **HKD** — so the AUD account's
  total_assets/cash came back in HKD. Added a per-account **base currency**, chosen at
  onboarding (`BrokerAccountCreateRequest.currency`, default USD, folded into
  `connection_config`; `Settings.opend_currency` fallback) and passed to
  `accinfo_query(currency=…)`. Account 62 set to USD; equity corrected from an illustrative
  ~$250,000 (HKD) to ~$32,000 (USD) — real figures redacted, ratio representative of the bug —
  `mode` corrected to `live`.
- **Per-position native currency, labeled** (`ba8742f` data layer, `391d062` display).
  Per the design decision (user-chosen), per-position prices stay in each holding's native
  currency (USD for US, AUD for ASX) rather than being FX-converted — but are now
  **labeled**. Added `Position.currency` / `Account.currency` (Alpaca → USD; Moomoo derived
  from the market prefix), a `positions_snapshot.currency` column (migration `fbdf8f40c65a`,
  default USD, validated on a real-DB copy), and currency labels in the daily/weekly email
  summary + untracked + holdings tables and the `/positions` API. The cross-currency % stays
  approximate against base-currency equity (exact for base-currency holdings — all targets
  are USD — with the row's label flagging the rest).

### Suggestion correctness — directional limits
A BUY limit must sit at/below the current price (a pullback), a SELL at/above. Two places
violated it (both surfaced as GOOG/BTC/BRK.B limits *above* market):
- **Draft generation** (`e60f434`): `select_anchor` picked the highest-confidence S/R level
  in a symmetric ±15% band with no direction check, so a "support" sitting just above market
  (a trailing EMA/pivot) became the BUY limit. The scored-levels path now filters supports
  to ≤ current and resistances to ≥ current.
- **Critic re-anchor** (`86284c1`): the review graph's `_find_level` accepted any level of
  the right method+type for the critic's `prefer_anchor`, re-anchoring a BUY onto an
  above-market "support" (BTC `pivot_weekly_S2` 32.77 vs 32.48 current) — bypassing the draft
  guard. `_find_level` is now direction-aware (uses `ctx.indicators[ticker].close`).

### Emails & market context
- **Broker name hardcoded** (`c87eb42`): the weekly email said *"Log into Alpaca to act"* on
  every account, including Moomoo. Now `{{ account_broker | capitalize }}`.
- **Stale Ticker Catch-Up** (`9413d41`): Tavily honors the `days` recency window only for
  `topic="news"`; the per-ticker/sector catch-ups used `topic="finance"`, which ignored it
  and surfaced a weeks-old "Bitcoin hit $73k" article in a later review. `_search` now also
  sends `time_range` (honored for all topics).

### Movers
- **Daily movers crashed** (`0047d53`): a shared article (a Micron piece tagged to both MU
  and MSFT) was `session.add`-ed twice → `UNIQUE constraint failed: news_event.url_hash` on
  the next autoflush, aborting the whole job (no email). Extracted a pure `_build_news_events`
  that dedupes by url_hash against the DB **and** within the run. First `tests/test_movers.py`.
- **Inflated week-over-week move** (`567ffb3`): "last week's close" used the close exactly 7
  days back — on a Monday run that lands two Fridays ago, so MU showed +37.8% (vs May 22) when
  the real move vs last Friday (May 29) is +6.6%, and 8 "movers" were really 4. Now uses
  `date_trunc('week', …)` to take the prior week's last trading day.

### Auto-trade & reconciliation — order lifecycle
The stale-order guard skipped accepted suggestions whose ticker had a prior
`accepted_for_routing` execution, but that DB status drifts from the broker. Fixed both ends:
- **Broker-aware stale-order guard — cancel-and-replace** (`e9bd724`). The guard now
  reconciles each prior order against the broker (LIVE only; DRY_RUN keeps the conservative
  block, never touching real orders): genuinely-open GTC → cancel + replace; already done
  (filled/canceled) → clear the stale row + proceed; broker status unknown → skip (avoid a
  duplicate). Verified live: the 4 skipped Alpaca orders (AMZN/GOOG/QQQ/VOO) cancelled-and-
  replaced cleanly (3 open cancelled, GOOG's filled row cleared).
- **Reconciliation matches fills by `broker_order_id`, not the broker string** (`5f8cf92`).
  The placement row could carry `alpaca_paper` (back-compat auto-trade entrypoint) while
  reconciliation passed the bare family `alpaca`, so the upsert missed the placement row and
  inserted a *duplicate* filled row — leaving the original stuck at `accepted_for_routing`
  (the GOOG stale row that wrongly blocked). Now matched by `broker_order_id` + account +
  `dry_run=False`; fills update the placement row in place. Fixes the stale-row class at the
  source (`order_execution.broker` is consumed only by this match).
- **Broker string + traded account sourced from the account row, not globals** (`1b77dff`,
  hardening follow-up to `5f8cf92`). The root cause of the drift above was the back-compat
  `run_auto_trade_job` trusting globals: it wrote `broker=settings.broker` (`alpaca_paper`)
  rather than the account's family string, *and* it ignored the account it was handed —
  resolving the *primary* instead. So `/admin/run-auto-trade?broker_account_id=N` would run
  the primary's suggestions through account N's adapter. Now a per-account wrapper
  (`run_auto_trade_job_for_account`) sources both the broker string and the traded
  `account_ref` from the `AccountInfo` (the family string — identical to what reconciliation
  writes), and the all-brokers loop + the admin endpoint both route through it.
  `settings.broker` no longer reaches the `order_execution` write path, so the column is
  self-consistent per account. No backfill: `order_execution.broker` is write-only after
  `5f8cf92` (no readers in `src/`), so historical drifted strings are inert. Two regression
  tests in `tests/test_auto_trade_routing.py` (placed row carries `account.broker`;
  per-account trigger trades that account, not the primary).

### API scoping
- **`/admin/reload-targets` honors `?broker_account_id`** (`98abe6b`) — it had ignored the
  scope param and reloaded all active accounts (the last B-API endpoint missing scoping;
  `cancel-all-orders` / `reset-week-suggestions` already had it). Idempotent, so the impact
  was benign, but now consistent (default all-active; an id targets one; 404 if inactive).

### Migrations added
`fbdf8f40c65a` — `positions_snapshot.currency` column (ADD COLUMN default `USD`; existing
rows backfill to USD; validated on a real-DB copy with downgrade round-trip).

## Test summary

| Milestone | Tests |
|---|---|
| Phase 4.8 close (after flaky-test fix) | 343 |
| Phase 4.9a Stage A + foundation hardening | 346 |
| Stage B (B1 mode isolation, B2 factory, B3 gap isolation, B-API scope/404) | 360 |
| Stage C (broker-account onboarding) | **364** |
| Smoke-test fixes (sync routing, stable primary, per-account targets, OpenD encryption) | **369** |
| Post-smoke live-operation fixes (currency, directional limits, movers, stale-order guard, reconciliation upsert) | **389** |
| Auto-trade write-path hardening (broker string + traded account from the account, not globals) | **391** |

`uv run pytest` → 391 passed, 1 skipped. `ruff check src/ tests/` clean. `mypy src/` → 30 pre-existing errors, no new ones. The d8589 + 6a4a + `fbdf8f40c65a` migrations were validated on a copy of the real DB (one `broker_account_id` group, counts preserved, all 5 columns NOT NULL, constraints survived the batch recreate, downgrade round-trips); fresh `alembic upgrade head` builds all 16 model tables.

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

The 2-broker smoke test was executed live on 2026-05-31 (see "Smoke test execution + hardening" above). Apply `v0.4.9a.0` once the remaining ⏳ rows (tonight's per-broker emails) look right:
1. ✅ Migration: existing single-broker rows carry through to one `broker_account_id` group (validated on a real-DB copy; the live restart applied the chain cleanly).
2. ✅ `POST /admin/broker-accounts` connects Moomoo (`account_ref=62`); `make_account_adapter` returns the right (encrypted Moomoo) adapter; it appears in `/health`.
3. ⏳ Daily report fires per active broker → two emails with `[nickname]` subjects (fires next weekday 16:15 ET).
4. ✅ Weekly suggestions → each broker its own email; a ticker held in both yields independent per-broker suggestions (re-run live for both accounts during the post-smoke fixes — 0 directional violations after `e60f434`/`86284c1`).
5. ✅ Promote Alpaca → LIVE leaves Moomoo OFF in `auto_trade_state` (verified: 61 LIVE, 62 OFF).
6. ✅ Stale-live-order guard exercised live (`e9bd724`): the 4 Alpaca orders blocked by a stale `accepted_for_routing` row cancelled-and-replaced cleanly on the *same* broker; cross-broker independence holds by `broker_account_id` scoping (the two-different-brokers allow path is covered by unit tests, not yet exercised live since Moomoo is OFF).
7. ✅ `data/targets/62.yaml` edited independently (gap for 62 uses its own 11 targets); `config/targets.yaml` still aliases the primary.
8. ✅ Soft-delete (`is_active=False`) excludes a broker from cron loops (used to hold 62 from a run; `/health` excluded it); its history stays queryable.
