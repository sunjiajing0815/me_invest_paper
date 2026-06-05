# ADR-0024 — Multi-Broker Single-User Data Model

**Status:** Accepted  
**Date:** 2026-05-30  
**Phase:** 4.9a

## Context

Phase 4.9a lets one user hold positions across multiple broker accounts at once (Alpaca + Moomoo first), with separate per-broker daily/weekly emails. The app was single-broker throughout: one adapter, one implicit account, and the auto-trade mode in a single `meta` key. Making it multi-broker raised several data-model and scoping decisions. Suggest-only holds across all brokers; auto-trade LIVE stays Alpaca-only.

## Decisions

### 1. `broker_account_id` partition key on every per-account table — dual-purpose `broker_account`, no new identity table

**Decision:** Add `broker_account_id` to `target_allocation`, `positions_snapshot`, `order_suggestion`, `order_execution`. The existing `broker_account` table (Integer PK, time-versioned cash/equity state, close-and-insert) becomes **dual-purpose**: it carries both identity (`nickname`, `is_active`, `connection_config`) and state. Because close-and-insert means the auto-increment `id` changes on every cash/equity change, a separate **`account_ref`** column (constant across an account's state rows) is the stable partition key; per-account tables reference `account_ref`, never `id`. `snapshot.py` carries `account_ref` + the identity columns forward when it close-and-inserts a new state row; the latest open row (`effective_to IS NULL`) is the source of truth.

**Rationale:** A separate `broker_account` identity table (UUID PK) was considered and rejected for this single-user phase: it adds joins everywhere and a second table to keep in sync, for a benefit (clean identity/state separation) that only matters at Phase 5 multi-tenant scale. `account_ref` gives a stable FK target without the second table. UUIDs were rejected too — the codebase is Integer-PK throughout, and Phase 5a will reconsider keys at the Postgres cut-over regardless.

### 2. `broker_account_id` is a plain column — no DB foreign key

**Decision:** `broker_account_id` carries no `ForeignKey` constraint; referential integrity is app-enforced.

**Rationale:** Matches the existing codebase convention (`OrderExecution.suggestion_id` has no FK either). SQLite FK enforcement is off by default and batch migrations fight FK constraints. The app is the single writer; it always sets a valid `account_ref`. A follow-up migration (`6a4a9fada1dc`) tightens the column to `NOT NULL` once every writer sets it.

### 3. Per-broker `auto_trade_state` + a per-broker soak ladder

**Decision:** Replace the single `meta.auto_trade_mode` key with an `auto_trade_state` table keyed by `account_ref` (mode + optional per-broker cap overrides). Each broker promotes through its own OFF → DRY_RUN → LIVE soak ladder independently — promoting Alpaca does **not** promote Moomoo. New brokers are seeded OFF and stay OFF until their own soak completes (see ADR-0014). The kill switch, guards, and spend caps are all scoped per `broker_account_id`.

**Rationale:** A shared mode would mean connecting a new broker silently inherits Alpaca's LIVE status — exactly the kind of surprise the suggest-only/soak discipline exists to prevent. Per-broker state makes "this broker is allowed to trade" an explicit, per-broker decision.

> **Migration ordering gate:** `d8589` deletes `meta.auto_trade_mode` and seeds `auto_trade_state`. The mode read path (`_get_mode`) was rewired to `auto_trade_state` in the same body of work (B1) before any restart, so a deploy doesn't silently fall auto-trade to OFF.

### 4. Cross-broker wash-sale is deliberately per-broker in 4.9a

**Decision:** The wash-sale guard (and all auto-trade guards) scope by `broker_account_id`. A loss sell in one broker does not block a buy of the same ticker in another broker.

**Rationale:** True wash-sale is an IRS concept across *all* accounts for substantially-identical securities — i.e., tax-lot accounting, which is explicitly Phase 6+. Pretending to handle it cross-broker here would be a half-measure that's wrong in subtle ways. Per-broker scoping is honest about the boundary; the cross-account/tax-lot version is deferred and documented, not silently approximated.

### 5. News, technical levels, and market context stay user-level

**Decision:** `news_event`, `sr_level` scoring, and `weekly_market_context` are **not** scoped by `broker_account_id`. One synthesis serves all brokers; the per-broker review graph reads the same user-level context but scales drafts within each broker's own gap/account.

**Rationale:** A news event about AAPL is a fact about AAPL regardless of how many brokers hold it. Duplicating news/levels/context per broker would multiply LLM cost and break the single audit story for no benefit — the user reads the news once.

### 6. Back-compat single-broker entrypoints alongside `*_all_brokers` loops

**Decision:** Each job keeps a single-broker entrypoint (resolves the primary account) and gains an `*_all_brokers` loop that fans out over `app.state.adapters`, isolating per-broker failures (`try/except … continue`). The scheduler and the default job-trigger endpoints use the loops; endpoints accept `?broker_account_id` to target one. Movers stays global (watchlist price moves are user-level).

> **Update (2026-06, 4.9b pulled forward):** the Friday weekly review now fans out per broker too — `run_weekly_review_all_brokers` sends one email per active account (subjects prefixed `[nickname]`), and `POST /admin/run-weekly-review` accepts `?broker_account_id` like the other triggers. The **user-level** weekly market context (Tavily/Sonnet synthesis) is built **once** over the union of active watchlists and shared across every account's review — it must not be rebuilt per account (it's the producer the suggestion engine consumes; see ADR-0020). Originally this was deferred to 4.9b as "primary-scoped in 4.9a"; it shipped early because Moomoo (auto-trade OFF, acted on manually) genuinely needs its own suggested-vs-acted review.

**Rationale:** The loops are the multi-broker path; the single entrypoints keep the call sites and tests that target one account simple, and give endpoints a clean "one account" mode. One broker's outage (e.g. IB Gateway down) must not abort the others' emails.

## Consequences

- Connecting a broker is adding rows (a `broker_account` identity row + an OFF `auto_trade_state` row via `POST /admin/broker-accounts`), not a schema change. Removing one is a soft-delete (`is_active=False`) — history is never destroyed.
- Phase 5a's "user_id on every row" migration is simpler because `broker_account_id` already partitions the per-account tables.
- Deferred to 4.9b: household target allocation and the consolidated summary email. (Per-broker weekly review was pulled forward — see the Update above.) Deferred to Phase 6: tax-lot / cross-broker wash-sale. Deferred (own sub-phases): IBKR and Tiger adapters (ADR-0025/0026).
