# Soak-Window Work Report

Completion record for the soak-window backlog in `plans/soak_window_work_plan.md`. Tracks what
actually shipped (vs. the plan, which is the intended order). All work is on the existing
2-broker setup (61 Alpaca paper / LIVE auto-trade, 62 Moomoo / OFF) — no new brokers,
multi-tenant, or Postgres were touched, per the soak hard-scope rules.

**Last updated:** 2026-06-30.
**Baseline tag:** `v0.4.9a-hardened` (at `64c7958`, on origin).

---

## Status summary

- **Priority 0 — Operational hygiene: ✅ complete** (P0.1–P0.6). Pushed to origin.
- **Priority 1 — Bounded user-value features: ✅ complete** (P1.1–P1.6). **4 commits local-only,
  not yet pushed** (`6816522`, `6be9b8f`, `c6daa07`, `ba2121e`).
- **Priority 2+ (rebalance plumbing, household view, calendar prompts, observability, polish): not
  started.**

Full test suite at the end of P1: **520 passed, 1 skipped**; `ruff` + `mypy` clean. Each code
batch was rebuilt + restarted to health 200 on the Docker deployment.

---

## Priority 0 — Operational hygiene (✅, pushed)

| Item | What shipped | Commit |
|---|---|---|
| P0.1 | Tag `v0.4.9a-hardened` bounding the post-4.9a window (incl. the WAL fix) | `64c7958` (tag) |
| P0.2 | `scripts/audit_integrity.py` (read-only consistency audit) + findings `plans/data_integrity_audit_2026-06.md` | `55309f7` |
| P0.3 | Weekly `db_backup` job (`VACUUM INTO data/backups/`) + `operational_runbook.md` + verified scratch-volume restore | `a2d5ad2` |
| P0.4 | ADR-0033 (snapshot one-`ts`-per-batch contract) — `docs/adr/0033-*.md` + product_plan pointer | `ae77dd4` |
| P0.5 | CLAUDE.md gotcha #12 expanded — pragma-audit-on-library-swap (the WAL root cause) | `ae77dd4` |
| P0.6 | CLAUDE.md gotcha #29 — `targets.yaml` band must bracket `pct` | `ae77dd4` |

**P0.2 finding (open, low-impact):** the WAL-loss canary (DB target-hash vs YAML) passed for both
accounts — no TSLA-class divergence elsewhere. One WARN: account 61 has **pre-2026-06-13
split-batch snapshots** (the §9 `fffa6dc` backfill collapsed only account 62). Current drift uses
`MAX(ts)` = the coherent latest batch, so live reporting is correct; an optional one-time backfill of
account 61's old batches is **deferred to P3.2** (only matters if a feature re-renders historical drift).

**P0.3 operational note:** the live DB is on the `me_invest_dbdata` Docker volume, **not**
`./data/investor.db`. Backups land in `data/backups/` (bind mount → Time-Machine-covered). Restore
procedure + manual-backup commands are in `operational_runbook.md`.

---

## Priority 1 — Bounded user-value features (✅, NOT pushed)

Listed in numeric order; the "Batch" column shows the actual shipping order (smallest-risk first).

| Item | What shipped | Commit | Batch |
|---|---|---|---|
| P1.1 | Holdings glossary footer — curated `services/ticker_names.py` + `holdings_glossary` macro in daily / weekly-suggestions / weekly-review emails (the BTC-is-an-ETF fix, ADR-0029) | `6be9b8f` | 2 |
| P1.2 | `RECONCILIATION_MAX_LOOKBACK_DAYS=30` caps the reconciliation window + WARNING when a zombie GTC would widen it | `6816522` | 1 |
| P1.3 | Manual-broker-UI-cancel inference — `order_execution.cancelled_at` (migration `a1b2c3d4e5f6`) + `infer_manual_cancels()` in the daily reconciliation job; flips a still-`accepted` suggestion to `cancelled` after a 24h grace window with a no-newer-open-execution guard (closes ADR-0032's gap) | `ba2121e` | 4 |
| P1.4 | Un-accept confirm **GET** renders last-known status from `order_execution` (no broker call — prefetch-safe); POST still re-queries the broker | `6816522` | 1 |
| P1.5 | `sentiment_canary()` logs a WARNING when the latest market context is recent but has NULL VIX/F&G (CNN scrape degraded, ADR-0030); runs in the daily job | `6816522` | 1 |
| P1.6 | Dividend-adjustment decision: **keep `Adjustment.SPLIT`** — `scripts/compare_dividend_adjustment.py` showed ≤5.6% NEE / <2% others swing-low drift, immaterial vs the ~15% anchor band. ADR-0029 updated; no re-backfill | `c6daa07` | 3 |

**P1.3 verification on the live DB:** migration applied (column present); the inference flips **0**
today — the 39 pre-existing `broker_cancelled` rows have NULL `cancelled_at`, so nothing is
retroactively flipped. It only acts on cancellations recorded after this deploy.

**Design choices honored:** glossary footer (no per-table clutter); `cancelled_at` + 24h grace +
no-open-execution guard preserves the deliberate cancel-and-re-place flow; P1.6 closed by analysis
rather than a speculative re-backfill; P1.5 is the WARNING canary only (email-footer UX is P5.3);
P1.2 surfaces zombie GTCs without auto-cancelling.

---

## New settings introduced (all have defaults; optional)

- `RECONCILIATION_MAX_LOOKBACK_DAYS=30` (P1.2)
- `MANUAL_CANCEL_INFERENCE_HOURS=24` (P1.3)
- `SENTIMENT_CANARY_MAX_AGE_DAYS=7` (P1.5)
- `BACKUP_ENABLED=true`, `BACKUP_DIR=data/backups`, `BACKUP_KEEP=8` (P0.3)

## New scheduled job

- `db_backup` — Sun 02:00 ET (P0.3).

---

## Outstanding follow-ups

- **Push pending:** the 4 P1 commits are local-only on `main`, ahead of origin.
- **Account-61 historical snapshot backfill** (from P0.2): deferred to P3.2; low impact.
- **P1.5 email-footer UX** ("sentiment temporarily unavailable" line): deferred to P5.3.
- **P2+ not started:** rebalance plumbing (`target_change_event`, YAML-edit guardrails, funds
  detection), household view, calendar prompts, structured logs / Sentry / runbook expansion, polish.

## How to re-verify

- Integrity audit: `docker compose exec app uv run python scripts/audit_integrity.py`
- Dividend re-check (if a high-yield ETF is added): `… scripts/compare_dividend_adjustment.py`
- Backup + restore: see `operational_runbook.md`.
- Tests: `uv run pytest -q` (520 passed / 1 skipped at report time).
