# Data-Integrity Audit — 2026-06-23 (P0.2, post-WAL-fix)

Read-only audit to **bound the unknown** after the ADR-0026 SQLite-WAL silent-data-loss fix
(latent Phase 3b → 2026-06-18). Run via `scripts/audit_integrity.py` against the live
named-volume DB (`/app/db/investor.db`). See `plans/soak_window_work_plan.md` P0.2.

**Bottom line: 0 FAIL, 1 WARN (historical, low-impact). No current data loss detected.** The
WAL-loss canary (DB target hash vs YAML) passes for both accounts, so the TSLA-class
divergence is not present anywhere else.

## Automated consistency checks

| Check | Result |
|---|---|
| `target_allocation` — one open row per (account, ticker) | ✅ PASS |
| `target_allocation` — DB hash == account YAML (WAL-loss canary) | ✅ PASS (61 & 62) |
| `broker_account` — one open state row per `account_ref` | ✅ PASS |
| `order_suggestion` — status domain + accepted⇒acted_at | ✅ PASS |
| `order_execution` — (broker_order_id, broker) unique | ✅ PASS |
| `positions_snapshot` — one ts per sync batch (ADR-0033) | ⚠ WARN (acct 61, historical) |

### The one WARN — account 61 pre-2026-06-13 split snapshot batches

Account 61 (Alpaca) has **6 pairs of snapshot timestamps <5 s apart**, all **before**
`fffa6dc` (the §9 one-`ts`-per-batch fix, 2026-06-13). They show the per-row-`as_of`
signature — e.g. `2026-05-28 20:15:01.732161 (1 row)` + `…732244 (7 rows)`: one sync split
across two microsecond timestamps.

**Root cause:** the pre-`fffa6dc` `take_snapshot` used per-position `p.as_of`; the §9 fix's
one-time historical backfill collapsed **account 62 only** — account 61's pre-fix history was
left as-is (its drift table wasn't visibly broken at the time).

**Impact: low / none for current operations.**
- `alloc_drift.sql` selects the **latest** batch via `ts = MAX(ts)`; the latest snapshots are
  post-fix and coherent, so the weekly-review drift table is correct today.
- The split batches would only produce wrong drift if a feature **re-renders a historical
  week** for account 61 — no current feature does. The future **P3.2 annual review**
  (year-over-year drift) would; address before that lands.

**Recommended follow-up (optional, not part of this read-only audit):** a one-time backfill
collapsing account 61's pre-06-13 split batches to one `ts` each (same procedure used for
account 62 in `fffa6dc`). Deferred until P3.2 or on request — it's a historical-data mutation
with no current-operations benefit.

## Recent writes (eyeball vs memory / broker UI / email)

Captured for operator review; nothing here looked anomalous:

- **Accounts:** 61 Alpaca (LIVE), 62 Moomoo (OFF) — matches the soak ladder.
- **Target reloads:** acct 62 last reloaded 2026-06-18 (13 tickers — the TSLA add); acct 61
  last 2026-06-01 (10 tickers). Earlier reloads tracked back to 2026-04-28. Consistent with the
  known target-edit history.
- **Auto-trade promotions:** 61 `OFF→DRY_RUN→LIVE` on alpaca_paper (2026-05-20), `→LIVE`
  reconfirmed 2026-05-26; 62 never promoted (OFF). Matches `auto_trade_state` (61 LIVE / 62 OFF).
- **Recent suggestion actions:** BTC buy → filled (06-24), TQQQ buy → cancelled (06-22, the
  un-accept we tested), GOOG buy → filled, a batch of 06-22 expiries. All plausible.
- **Status distribution:** expired 42, filled 11, pending 3, accepted 2, rejected 1,
  cancelled 1.

**Operator action:** skim the "recent writes" section of a live run against your memory /
Alpaca UI / email archive. If every target edit, promotion, and accept you remember is present
and correctly stated, the bound is confirmed — no silent loss beyond the already-fixed TSLA case.

## How to re-run

```bash
docker compose cp scripts/audit_integrity.py app:/app/scripts/audit_integrity.py  # if not rebuilt
docker compose exec app uv run python scripts/audit_integrity.py
```
(After the next image rebuild the script is baked in; the `cp` is only needed between rebuilds.)
