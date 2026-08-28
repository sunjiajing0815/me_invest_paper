# Soak-Window Work Report

Completion record for the soak-window backlog in `plans/soak_window_work_plan.md`. Tracks what
actually shipped (vs. the plan, which is the intended order). All work is on the existing
2-broker setup (61 Alpaca paper / LIVE auto-trade, 62 Moomoo / OFF) — no new brokers,
multi-tenant, or Postgres were touched, per the soak hard-scope rules.

**Last updated:** 2026-06-30.
**Baseline tag:** `v0.4.9a-hardened` (at `64c7958`, on origin).

---

## Status summary

- **Priority 0 — Operational hygiene: ✅ complete** (P0.1–P0.6). On origin.
- **Priority 1 — Bounded user-value features: ✅ complete** (P1.1–P1.6). On origin.
- **Priority 2 — Rebalance plumbing (Wave A): ✅ complete** (P2.1–P2.3). On origin.
  - **Wave B (household unit, P2.4 + P2.5): deferred** by decision (biggest/most product-y chunk).
- **Priority 3+ (calendar prompts, observability, polish): not started.**

Full test suite after P2 Wave A: **532 passed, 1 skipped**; `ruff` + `mypy` clean. Each code batch
was rebuilt + restarted to health 200; all three migrations round-tripped on a scratch DB and
applied to the live volume DB on boot.

---

## Priority 0 — Operational hygiene (✅)

| Item | What shipped | Commit |
|---|---|---|
| P0.1 | Tag `v0.4.9a-hardened` bounding the post-4.9a window (incl. the WAL fix) | `64c7958` (tag) |
| P0.2 | `scripts/audit_integrity.py` (read-only consistency audit) + findings `plans/data_integrity_audit_2026-06.md` | `55309f7` |
| P0.3 | Weekly `db_backup` job (`VACUUM INTO data/backups/`) + `operational_runbook.md` + verified scratch-volume restore | `a2d5ad2` |
| P0.4 | ADR-0033 (snapshot one-`ts`-per-batch contract) | `ae77dd4` |
| P0.5 | CLAUDE.md gotcha #12 — pragma-audit-on-library-swap (WAL root cause) | `ae77dd4` |
| P0.6 | CLAUDE.md gotcha #29 — `targets.yaml` band must bracket `pct` | `ae77dd4` |

**P0.2 finding (open, low-impact):** WAL-loss canary clean for both accounts. One WARN: account 61
has pre-2026-06-13 split-batch snapshots (the §9 backfill collapsed only account 62). Current drift
uses `MAX(ts)` = coherent latest batch → live reporting correct; optional account-61 backfill
deferred to P3.2.

**P0.3 note:** the live DB is on the `me_invest_dbdata` Docker volume (not `./data/investor.db`);
backups land in `data/backups/` (Time-Machine-covered). See `operational_runbook.md`.

## Priority 1 — Bounded user-value features (✅)

| Item | What shipped | Commit |
|---|---|---|
| P1.1 | Holdings glossary footer — `services/ticker_names.py` + `holdings_glossary` macro (daily/weekly emails; the BTC-is-an-ETF fix) | `6be9b8f` |
| P1.2 | `RECONCILIATION_MAX_LOOKBACK_DAYS=30` window cap + zombie-GTC WARNING | `6816522` |
| P1.3 | Manual-broker-UI-cancel inference — `order_execution.cancelled_at` (migration `a1b2c3d4e5f6`) + `infer_manual_cancels()` (24h grace + no-open-execution guard; closes ADR-0032 gap) | `ba2121e` |
| P1.4 | Un-accept GET renders last-known status from DB (no broker call — prefetch-safe); POST re-queries | `6816522` |
| P1.5 | `sentiment_canary()` daily WARNING when latest context lost VIX/F&G (CNN scrape degraded, ADR-0030) | `6816522` |
| P1.6 | Dividend-adjustment decision: **keep `Adjustment.SPLIT`** (≤5.6% NEE / <2% others drift, immaterial; ADR-0029 updated) | `c6daa07` |

## Priority 2 — Rebalance plumbing, Wave A (✅)

| Item | What shipped | Commit |
|---|---|---|
| P2.1 | `target_change_event` audit table (migration `b2c3d4e5f6a7`); `load_targets_into_db` writes a source-tagged old→new diff + `max_shift_pp` on every applied edit; `compute_target_shifts()` helper | `6e786c5` |
| P2.2 | **Warn-only** large-edit guardrail — a reload shifting a ticker > `TARGET_EDIT_WARN_THRESHOLD_PCT` (10) still applies but logs a WARNING + sends a notice email. (No magic-link/held state — git pre-commit was infeasible: `data/targets/` is gitignored.) | `6e786c5` |
| P2.3 | Funds-flow detection — `funds_event` table (migration `c3d4e5f6a7b8`), cash-flow heuristic (`services/funds.py`), daily 18:00 ET job + email, **ADR-0035** | `ed66b69` |

**P2.3 verification:** dry `detect_funds_flow` returns None for both accounts (no false positive);
`funds_detection` job registered (Mon–Fri 18:00 ET). Known limitation (ADR-0035): dividends/fees
below the $500 floor are ignored; a large one can read as a deposit — surfaced for the user to
interpret.

---

## New settings introduced (all have defaults; optional)

- `RECONCILIATION_MAX_LOOKBACK_DAYS=30` (P1.2), `MANUAL_CANCEL_INFERENCE_HOURS=24` (P1.3),
  `SENTIMENT_CANARY_MAX_AGE_DAYS=7` (P1.5)
- `TARGET_EDIT_WARN_THRESHOLD_PCT=10` (P2.2), `FUNDS_DETECTION_THRESHOLD_USD=500` (P2.3)
- `BACKUP_ENABLED=true`, `BACKUP_DIR=data/backups`, `BACKUP_KEEP=8` (P0.3)

## New scheduled jobs

- `db_backup` — Sun 02:00 ET (P0.3).
- `funds_detection` — Mon–Fri 18:00 ET (P2.3).

## Migrations added

- `a1b2c3d4e5f6` order_execution.cancelled_at (P1.3) · `b2c3d4e5f6a7` target_change_event (P2.1)
  · `c3d4e5f6a7b8` funds_event (P2.3).

## ADRs

- Written: ADR-0033 (snapshot one-ts, P0.4), ADR-0035 (funds detection, P2.3); ADR-0029 updated
  (dividend decision, P1.6).
- Reserved for Wave B: ADR-0034 (household target), ADR-0037 (email aggregation).
  (ADR-0036 was taken by the paper-only public build; email aggregation moves to 0037.)

## Outstanding follow-ups

- **Wave B (household), deferred:** P2.4 `household_target_allocation` + `services/household_summary.py`
  (ADR-0034); P2.5 consolidated emails + `email_aggregation` toggle (ADR-0037).
- **Account-61 historical snapshot backfill** (from P0.2): deferred to P3.2; low impact.
- **P1.5 email-footer UX** ("sentiment temporarily unavailable" line): deferred to P5.3.
- **Priority 3+ not started:** calendar prompts (quarterly/annual/tax-year-end), structured logs /
  Sentry / runbook expansion, polish.

## How to re-verify

- Integrity audit: `docker compose exec app uv run python scripts/audit_integrity.py`
- Funds detection (dry): `detect_funds_flow(...)` per account → None today; job at 18:00 ET.
- Target audit: a target reload writes a `target_change_event`; >10pp logs a WARNING + emails.
- Backup + restore: see `operational_runbook.md`. Tests: `uv run pytest -q` (532 passed / 1 skipped).
