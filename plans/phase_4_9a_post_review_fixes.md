# Phase 4.9a Post-Review Fixes — Completion

## Summary

After the 2-broker smoke test and the post-smoke live-operation fixes (recorded in
`phase_4_9a_completion.md` → "Post-smoke-test fixes — live operation"), a review pass over
the completion report and the changed code surfaced one more correctness bug, one
write-path hardening, and a round of cleanup — plus two flagged concerns that turned out
to be **non-bugs** (verified, documented, no code change). All landed on `main` and were
redeployed live. Tests stayed at **391 passed / 1 skipped** (the auto-trade routing tests
added two; the cleanup removed one stale duplicate and the weekly-review fix added one
regression — net flat with better coverage).

Each item below traces to a specific reviewer observation.

---

## What was fixed

### Fix 1 — Auto-trade broker-string drift + cross-account misroute (CRITICAL) — `1b77dff`

**Reviewer concern:** The reconciliation broker-string mismatch (`5f8cf92`, prior session)
was the sharpest bug in the post-smoke batch — a placement row carrying `broker="alpaca_paper"`
while reconciliation wrote `"alpaca"` made the upsert insert a *duplicate* filled row and
leave the original stuck at `accepted_for_routing` (the GOOG ghost the stale-order guard
then blocked against). `5f8cf92` made the *match key* tolerate the mismatch
(`broker_order_id` + account); the reviewer asked whether the loose `broker`-column
semantics should also be closed at the source (invariant test, or drop `broker` from the
key entirely).

**Investigation found two bugs with one root cause** — the back-compat `run_auto_trade_job`
trusted globals instead of the account it was handed:
- **Broker-string drift:** it wrote `broker=settings.broker` (`"alpaca_paper"`), while every
  other writer (cron loop, reconciliation) used `broker_account.broker` (`"alpaca"`, the
  family). `order_execution.broker` is write-only after `5f8cf92` (no readers in `src/`),
  but rows were still self-inconsistent per account.
- **Cross-account misroute:** `POST /admin/run-auto-trade?broker_account_id=N` resolved
  account N's adapter, then `run_auto_trade_job` *ignored* N and resolved the **primary**.
  Pointing the trigger at Moomoo (62) would have run Alpaca's (61) suggestions through the
  Moomoo adapter.

**Changes:**
- `jobs/auto_trade.py`: added `run_auto_trade_job_for_account(settings, adapter, emailer, account)`
  which sources both the broker string and the traded `account_ref` from the `AccountInfo`
  (the family string — identical to what reconciliation writes). `run_auto_trade_job_all_brokers`
  and `run_auto_trade_job` (now takes an optional `account`, default = primary) both route
  through it. `settings.broker` no longer reaches the `order_execution` write path.
- `main.py`: the per-account `/admin/run-auto-trade` branch passes `account=acct`.
- No backfill — historical drifted broker strings are inert (write-only column); new rows
  are consistent per account.

**Tests added:** `tests/test_auto_trade_routing.py` (new) — (1) the placed row carries
`account.broker`, not `settings.broker`; (2) triggering a specific account trades that
account, not the primary.

---

### Fix 2 — Weekly review reported auto-trade OFF for every account (HIGH) — `5eaa0b0`

**Reviewer concern:** Surfaced during a stale-test audit (Fix 3). The weekly-review tests
build the `WeeklyReview` dataclass directly and the one `_build_review` test never asserted
the mode — so the mode-sourcing logic was effectively untested.

**Problem:** `jobs/weekly_review.py` read `session.get(Meta, "auto_trade_mode")`, but the
`d8589` migration **deletes** that key (mode moved to the per-account `auto_trade_state`
table). The lookup therefore always returned `None` → `"OFF"`, so every Friday weekly-review
email reported auto-trade **OFF** even with Alpaca (61) **LIVE**.

**Changes:**
- `jobs/weekly_review.py`: replaced the `Meta` read with `_get_mode(session, broker_account_id)`
  (the review is primary-scoped); removed the now-unused `Meta` import.

**Tests added:** `test_auto_trade_mode_sourced_from_auto_trade_state_not_meta` — seeds
`AutoTradeState(broker_account_id=1, mode="LIVE")` and asserts `_build_review(...)` reports
`"LIVE"`.

---

### Fix 3 — Stale duplicate test + 3 dead back-compat entrypoints (LOW / cleanup) — `d05e2a7`

**Reviewer request:** "check all current tests — are there any stale tests we can clean up?"
A full sweep of all 37 test files found the suite healthy (no vacuous tests, no leftover
`Meta` auto-trade seeding, config/factory tests current). Two cleanup items:

**Problem A — stale duplicate test:** `test_no_meta_row_defaults_to_off` was a strict
duplicate of `test_default_off_does_nothing` (same "no `auto_trade_state` row" setup; the
latter asserted strictly more) **and** its name referenced the `meta` key `d8589` deleted.

**Problem B — dead code:** the singular `run_daily_report`, `run_weekly_suggestions`, and
`run_daily_reconciliation` back-compat entrypoints had **zero callers** in `src/` or tests
(production uses the `*_all_brokers` / `*_for_account` variants everywhere; `run_auto_trade_job`
is the only singular one still live, via the per-account admin trigger).

**Changes:**
- `tests/test_auto_trade.py`: collapsed the two OFF tests into one accurately-named
  `test_no_auto_trade_state_row_defaults_to_off` documenting the absent-row→OFF safety
  fallback (CLAUDE.md gotcha 17).
- `jobs/daily_report.py`, `jobs/weekly_suggestions.py`, `jobs/reconciliation.py`: removed the
  three dead entrypoints; dropped the now-unused `resolve_primary_account_ref` import in
  `reconciliation.py`.

---

## Follow-up reviewer notes — all closed (no code change)

**Naive 7-day "last week" date math (`timedelta(days=7)` audit).** The movers fix (`567ffb3`,
prior session) replaced "exactly 7 days back" with `date_trunc('week', …)` because a Monday
run lands two Fridays back, and holidays/DST drift the reference day. The reviewer asked to
grep `timedelta(days=7)` / `timedelta(weeks=1)` for the same anti-pattern elsewhere. **Swept
every `timedelta` site, every `.sql` file, and every inline DuckDB `price_bar` query — the
bug does not recur.** Every other site is one of four safe forms: (1) snap-to-Monday via
`- d.weekday()`; (2) Monday-aligned `week_of` key matched against the stored Monday column;
(3) "latest on/before boundary" via `MAX(date) WHERE date <= :x` / `ORDER BY date … LIMIT n`
(e.g. `alloc_drift.sql`, which is the one place that *could* have had the bug and already does
it right); (4) intentional calendar-day windows (wash-sale 30d, promotion soak, news/staleness
lookbacks). DST isn't a live risk — all day-arithmetic is on `date` objects or tz-aware UTC,
and only APScheduler's ET cron triggers handle local time (it adjusts for DST itself). One
benign mention: `reconciliation.py:42` `now - timedelta(days=7)` is the cold-start fill
lookback (every later run uses the persisted last-run timestamp) — calendar-based by design,
not a trading-day proxy. **No change warranted.**

**`broker_account_id = 59` vs `account_ref = 61` in the Stage-A validation paragraph
(`c61559f`).** The reviewer flagged that the completion report's Stage-A validation said the
single `broker_account_id` group `= 59` while the smoke-test section says Alpaca is
`account_ref=61` — reading as either a contradiction or a migration that backfilled
`broker_account.id` instead of `account_ref` (which would orphan every per-account row on the
next close-and-insert). **Verified against the live DB: not a bug.** The migration sets
`account_ref` **and** every per-account `broker_account_id` to `jane_ref` (the origin row's
`id`) in one transaction, so they're equal by construction; the literal is non-deterministic
("the origin id"). The Stage-A validation ran on an earlier *copy* whose origin row was
`id=59`; the live migration ran later at `id=61`. Live query confirms every per-account
`broker_account_id ∈ {61, 62}` matches a live (`effective_to IS NULL`) `account_ref`, **zero
orphans** — and the Alpaca account has since close-and-inserted from origin `id=61` to live
`id=74` with `account_ref` unchanged at `61`, which is exactly the property the model
guarantees. Reworded the paragraph to state the invariant and note the live value is `61`.

---

## Deployment

Rebuilt and redeployed the live container on HEAD `c61559f` (`docker compose up -d --build` —
the image bakes the source, so a plain restart would have kept the prior code). Startup
clean: `Alembic migrations applied`, both adapters connected (Moomoo with RSA encryption +
Alpaca paper), all 8 scheduler jobs registered, health `healthy`. `/health` confirms the
per-account state intact and modes correct: **61 alpaca LIVE** (10 targets), **62 moomoo OFF**
(12 targets).

---

## Test summary

| Milestone | Tests |
|---|---|
| Phase 4.9a post-smoke fixes (completion report) | 389 |
| + Auto-trade write-path routing tests (`1b77dff`) | 391 |
| − stale OFF duplicate, + weekly-review regression (`5eaa0b0`, `d05e2a7`) | **391** |

`uv run pytest` → 391 passed, 1 skipped. `ruff check src/ tests/` clean. `mypy src/` → 30
pre-existing errors, no new ones.

---

## Files changed

| File | Fix |
|---|---|
| `src/investor/jobs/auto_trade.py` | 1 |
| `src/investor/main.py` | 1 |
| `tests/test_auto_trade_routing.py` (new) | 1 |
| `src/investor/jobs/weekly_review.py` | 2 |
| `tests/test_weekly_review.py` | 2 |
| `tests/test_auto_trade.py` | 3 |
| `src/investor/jobs/daily_report.py` | 3 |
| `src/investor/jobs/weekly_suggestions.py` | 3 |
| `src/investor/jobs/reconciliation.py` | 3 |
| `plans/phase_4_9a_completion.md` | docs (Fix 1 recorded `cffeb70`; Stage-A clarification `c61559f`) |

---

## Commits

| Commit | Subject |
|---|---|
| `1b77dff` | fix(auto-trade): source broker string + traded account from the account, not globals |
| `cffeb70` | docs(4.9a): record auto-trade write-path hardening in completion report |
| `5eaa0b0` | fix(weekly-review): source auto_trade_mode from auto_trade_state, not the deleted meta key |
| `d05e2a7` | chore: drop a stale duplicate test + 3 dead back-compat job entrypoints |
| `c61559f` | docs(4.9a): clarify Stage-A validation broker_account_id value (59 was the copy; live is 61) |

---

## Pre-tag punch list (toward `v0.4.9a.0`)

- [ ] A clean week of scheduled runs with no new surprises (daily / weekly / movers / recon / auto-trade)
- [ ] First post-fix auto-trade LIVE pass on Alpaca (61) places/handles orders cleanly on the new routing path
- [ ] First post-fix Friday weekly-review email reports the correct per-account auto-trade mode (61 LIVE)
- [ ] Moomoo (62) stays OFF across the week; its own soak ladder not yet started
