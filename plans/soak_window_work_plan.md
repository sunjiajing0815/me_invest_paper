# Soak-Window Work Plan (2026-06-18 → ~few months)

A curated subset of the open backlog — drawn from `post_4_9a_cleanup.md`, the parked
`phase_4_9b_guide.md` and `phase_4_9c_guide.md`, and the parked `phase_5_guide.md` —
selected for execution **during the extended soak window** while we observe the
post-4.9a hardening batch in steady state.

**Last updated:** 2026-06-18.

---

## Context

- 4.9a + post-4.9a hardening (2026-06-03 → 06-18) is live on `main`, running on **Alpaca paper + Moomoo (real funded, suggest-only)**.
- 4.9b (household + rebalance), 4.9c (IBKR + Tiger adapters), and Phase 5 (multi-tenant productization) are all **🅿️ Parked** in `product_plan.md`.
- The reason to soak is that the post-4.9a batch landed eight workstreams over 16 days, including a **silent data-loss fix** (ADR-0026 SQLite WAL) that had been latent since Phase 3b. Adding more surface area while observing steady-state behaviour dilutes the soak signal.

## Goal of this plan

Keep the product evolving on the **existing 2-broker setup**: bounded, high-value, low-risk work that improves the solo deployment without expanding what's being soaked. When the soak ends cleanly, 4.9c and Phase 5 resume from a foundation that is operationally tighter than today's.

## Hard scope rules (don't drift)

These are **out of scope for the soak window** — defer to 4.9c / Phase 5 / Phase 6:

- ❌ New broker adapters (IBKR, Tiger). Parked in 4.9c.
- ❌ Multi-tenant data model (`user_id` everywhere, application-layer scope, RLS). Parked in Phase 5a.
- ❌ SQLite → Postgres migration. Phase 5a.
- ❌ Auth wiring (Supabase, JWT, magic-link auth tokens distinct from existing target-edit tokens). Phase 5b.
- ❌ React dashboard / Vite frontend. Phase 5b.
- ❌ Legal copy / attestation flow. Phase 5c.
- ❌ Pillow → external chart service swap, or new chart types beyond the donut. Out of scope unless the donut breaks.
- ❌ Encrypted broker credentials (envelope encryption). Phase 5a.
- ❌ Per-user external-service keys (Tavily, Finnhub, Anthropic). Phase 5a.

If a task seems to require any of the above, **stop and reframe** — it doesn't belong in this plan.

## Conventions for coding agents reading this plan

Each task below has:

- **What** — one-line scope description.
- **Why** — why it's worth doing now, in the soak window.
- **Scope** — what's in / out of this specific task.
- **Depends on** — sequencing prerequisites.
- **Effort** — rough estimate (hours / days).
- **Refs** — primary documents to read for context.

Pick a priority tier and execute top-to-bottom within it; don't skip ahead across tiers without surfacing the trade-off.

---

## Priority 0 — Operational hygiene (do first, before any feature work)

These are gates the soak baseline needs in place. None are large. All bound the next reviewer's work surface.

### P0.1 — Tag `v0.4.9a-hardened` — ✅ done (2026-06-19; tagged `64c7958`, pushed to origin)

**What.** Tag `main` HEAD as `v0.4.9a-hardened` (or `v0.4.9a.1`) and push.
**Why.** Post-4.9a is now 16 days of un-tagged change including a silent-data-loss fix (ADR-0026). If a subtle bug surfaces during the soak, the bisect surface is currently the whole window. The tag bounds the rollback unit and defines the post-soak diff cleanly.
**Scope.** Tag only. No version-string bump in code unless a `__version__` already exists somewhere.
**Depends on.** Nothing.
**Effort.** 5 minutes.
**Refs.** `post_4_9a_cleanup.md` open-items; ADR-0026.

### P0.2 — Retroactive data-integrity audit (post-WAL fix) — ✅ done (2026-06-23; `scripts/audit_integrity.py` + `plans/data_integrity_audit_2026-06.md`; 0 FAIL, 1 low-impact WARN)

**What.** Pick a sample of writes you remember making across the Phase 3b → 2026-06-18 window (target edits, mode promotions, suggestion accepts, broker-account onboards) and verify the live DB matches your memory.
**Why.** The WAL silent-loss bug was latent for months. The TSLA target reload that surfaced it was one case; the audit is the only thing that bounds the unknown. ADR-0026 §"Open follow-ups" explicitly calls this out.
**Scope.** Read-only audit. SQL queries + cross-reference against your own memory and any external records (broker UI history, email archive). Produce a short report of any mismatches found.
**Depends on.** P0.1 (tag the baseline first).
**Effort.** 1–2 hours.
**Refs.** ADR-0026; `post_4_9a_cleanup.md` "High-priority operational concerns".

### P0.3 — Documented + verified backup-and-restore procedure for the named-volume DB — ✅ done (2026-06-23; `operational_runbook.md` + weekly `db_backup` job + verified scratch-volume restore)

**What.** Write a step-by-step procedure for backing up `me_invest_dbdata` and restoring it onto a fresh volume. Then execute the restore against a test volume to verify it actually works.
**Why.** The DB is no longer at `./data/investor.db` on the host filesystem — it lives in the Docker named volume. A `docker volume prune` wipes it; Time Machine on `./data/` no longer covers it. The recovery path being theoretical is the failure mode that bites the day you need it.
**Scope.** (a) Procedure document (target: `operational_runbook.md` at top level, new file). (b) Live verification: take a backup, create a scratch volume, restore, query a recent row to confirm. (c) Set a cron or calendar reminder for periodic backups (recommend: weekly + before any migration).
**Depends on.** P0.1.
**Effort.** ~half day including the verified restore.
**Refs.** ADR-0026 "Operational implications" section; `post_4_9a_cleanup.md` "High-priority operational concerns".

### P0.4 — Write ADR-0033 (snapshot one-`ts`-per-batch contract) — ✅ done (2026-06-23; `docs/adr/0033-snapshot-one-ts-per-batch.md` + product_plan pointer)

**What.** Codify the broker-adapter invariant that every row in a snapshot batch must share one `ts`. The `take_snapshot` service layer enforces it today (using `account.as_of` for all rows), but the contract isn't documented anywhere readable.
**Why.** Future broker adapters in 4.9c (IBKR, Tiger) will land into this contract. Without documentation they'd repeat the §9 Moomoo bug — per-row `as_of` collapses `alloc_drift.sql`'s `ts = MAX(ts)` to a single row, producing all-zero drift tables.
**Scope.** Add ADR-0033 to `product_plan.md`'s Architecture Decision Records section. Reference `services/snapshot.py::take_snapshot`, `alloc_drift.sql`, and any other batch-aggregation queries that share the invariant. Update `post_4_9a_cleanup.md` to mark it ✅.
**Depends on.** Nothing (pure docs).
**Effort.** ~1 hour.
**Refs.** `post_4_9a_changes.md` §9; ADR-0033 reserved slot already documented in the live ADR sequence.

### P0.5 — Expand CLAUDE.md gotcha #12 (pragma audit on library swaps) — ✅ done (2026-06-23)

**What.** Add to gotcha #12: *"if you ever swap a sqlite-touching library, audit the pragmas it set and unwind them explicitly; don't assume a code-level swap reverts engine-level state."*
**Why.** The WAL bug existed because the langgraph SqliteSaver → MemorySaver migration in Phase 3b reverted the code path but not the engine-level pragma. The fail-fast WAL check on `init_db` prevents that specific recurrence; the broader lesson applies to `synchronous`, `cache_size`, `temp_store`, `foreign_keys`, etc.
**Scope.** One paragraph addition to CLAUDE.md. No code change.
**Depends on.** Nothing.
**Effort.** 5 minutes.
**Refs.** ADR-0026 "Open follow-ups" §2.

### P0.6 — Add CLAUDE.md gotcha: target-loader band-bracketing validation — ✅ done (2026-06-23; gotcha #29)

**What.** Add a gotcha: *"`targets.yaml` must satisfy `band_low ≤ pct ≤ band_high` per ticker — the loader enforces this. A target outside its band loaded fine pre-`1cec2db` and made the holding read perpetually 'under band'."*
**Why.** §10 of `post_4_9a_changes.md` records the fix; without a gotcha entry, the lesson lives only in the commit log. Re-introducing redundant-field drift is a low-frequency footgun that costs hours to debug when it hits.
**Scope.** One paragraph in CLAUDE.md gotchas.
**Depends on.** Nothing.
**Effort.** 5 minutes.
**Refs.** `post_4_9a_changes.md` §10.

---

## Priority 1 — Bounded user-value features (high-leverage, small-scope)

These are existing-broker features that add immediate value without expanding surface area in ways that complicate the soak signal.

### P1.1 — Ticker-name / fund-proxy annotation in emails

**What.** Surface a one-line ticker-name annotation alongside each ticker in the email tables — e.g. `BTC (Grayscale Bitcoin Mini Trust ETF)`, `VOO (Vanguard S&P 500 ETF)`, `JEPI (JPMorgan Equity Premium Income ETF)`.
**Why.** The `BTC` discovery (ADR-0029) — that the ticker was a Grayscale ETF, not crypto-spot — was a real cognitive mismatch. Annotation prevents the same class of bug going forward and reduces the cost of glancing at a daily email.
**Scope.** (In) extend `services/news.py`'s existing ticker→name map; thread through the shared email components (`_components.html.j2`); render in the levels / allocation / orders-this-week tables. (Out) fund-prospectus-quality descriptions; CIK linkage; SEC filings cross-reference — keep it to the trading-name string only.
**Depends on.** P0.1.
**Effort.** ~2 hours including tests.
**Refs.** ADR-0029 "References"; `post_4_9a_cleanup.md` "Open — specific implementation concerns".

### P1.2 — Reconciliation window upper bound

**What.** Add `RECONCILIATION_MAX_LOOKBACK_DAYS` setting (default 30) to cap how far back `services/reconciliation.py` looks for activity. Log a `WARNING` when capped.
**Why.** Post-4.9a commit `56438b6` fixed the late-GTC reconciliation by extending the lookback to the oldest still-open execution. Without an upper bound, a forgotten zombie GTC from 60 days ago will make reconciliation pull months of activities every run, slowly degrading cron timing.
**Scope.** (In) the setting, the cap-application logic, a unit test that an open execution older than the cap triggers the WARNING. (Out) automatic cancellation of zombie executions — surfacing is enough; the user decides.
**Depends on.** P0.1.
**Effort.** ~1 hour including test.
**Refs.** ADR-0026-era commit `56438b6`; `post_4_9a_cleanup.md` "Open — specific implementation concerns".

### P1.3 — Manual-broker-UI-cancel gap mitigation

**What.** Detect when reconciliation marks an execution `broker_cancelled` and the linked suggestion is still `accepted`. After N hours of no user action (via the un-accept link or admin endpoint), auto-mark the suggestion `cancelled` so auto-trade stops re-placing it the next morning.
**Why.** ADR-0032's "Known Gap" — a user who cancels in the broker UI without using the un-accept link leaves the suggestion `accepted`; auto-trade re-places it the next morning. The un-accept path closes the footgun *only when the user uses the link*. This task closes it for the broker-UI cancel case too.
**Scope.** (In) a new background job (or a step in the daily reconciliation loop) that finds `(execution.status='broker_cancelled', suggestion.status='accepted', execution.cancelled_at > N hours ago)` and flips the suggestion to `cancelled` with an audit note (`cancel_source='broker_ui_inferred'`). Setting: `MANUAL_CANCEL_INFERENCE_HOURS` (default 24). (Out) immediately marking on broker-cancel detection — the N-hour delay gives the user time to manually unaccept-and-explain.
**Depends on.** P0.1; understanding of ADR-0032's "Known Gap" section.
**Effort.** ~3 hours including test.
**Refs.** ADR-0032 "Known Gap"; `post_4_9a_cleanup.md` "Open — specific implementation concerns".

### P1.4 — `GET /suggestions/{sid}/unaccept` prefetch hardening

**What.** Render the un-accept confirmation page from the DB's last-known execution status instead of querying the broker on GET. Only query the broker on POST (the action endpoint).
**Why.** Microsoft 365 SafeLinks, Gmail link-preview, Slack unfurl all hit the GET URL on link-hover or email-scan — sometimes repeatedly. At solo scale this is benign; in any growth scenario it can rate-limit against the broker. ADR-0032 flags this as a "Negative" consequence; mitigation is bounded and worth doing now.
**Scope.** (In) refactor the GET endpoint to read execution status from `order_execution` row and label the page "as of <last-reconciliation timestamp>". POST endpoint unchanged (it re-queries the broker for the authoritative cancel decision). (Out) any change to the un-accept business logic, the HMAC signature, or the templates.
**Depends on.** P0.1; ADR-0032 understanding.
**Effort.** ~2 hours including test.
**Refs.** ADR-0032 "Consequences" negative bullet; `post_4_9a_cleanup.md` "Open — specific implementation concerns".

### P1.5 — CNN sentiment canary

**What.** A daily canary check that the last `weekly_market_context` row has non-NULL `vix` and `fear_greed_score`. If both have been NULL for > 7 days, log a `WARNING` and (optionally) include a "sentiment data unavailable" footer in the next weekly review email.
**Why.** ADR-0030 documents the CNN scrape fragility — if CNN changes anti-bot, the Market Sentiment widget silently hides. Silent absence reads as "neutral market", which itself is a misleading signal. The canary surfaces the failure mode before it has been quietly degraded for weeks.
**Scope.** (In) a small check in `jobs/weekly_review.py` or a dedicated daily job that queries the latest `weekly_market_context` row; emit `WARNING` if both metrics have been NULL for the threshold window. Optional: render a single "(sentiment data temporarily unavailable)" line in the email footer when canary trips. (Out) any change to the scrape itself, fallback to a paid feed, or new metric backfill logic.
**Depends on.** P0.1; ADR-0030 understanding.
**Effort.** ~2 hours including test.
**Refs.** ADR-0030 "Operational fragility contract"; `post_4_9a_cleanup.md` (consider adding as a new item).

### P1.6 — Dividend-adjustment decision on bars

**What.** Decide whether to extend `services/bars.py` from `Adjustment.SPLIT` to `Adjustment.ALL` (split + dividend) for the swing-low detectors that look back > 1 year. If yes, implement and re-backfill.
**Why.** High-dividend ETFs (SCHD ~3.5%/yr, JEPI ~7%/yr) drift mildly stale on multi-year backfills under SPLIT-only — accumulated dividends are no longer in the price the market trades at. ADR-0029 documents the deliberate SPLIT-only choice but flags this as a follow-up. The soak window is the right time to gather a few weeks of "would a dividend-adjusted swing-low have produced a different suggestion?" data before committing.
**Scope.** (Phase A — decide) Compare current swing-low S/R levels for SCHD, JEPI, and any high-dividend tickers on your watchlist against what they'd be under `Adjustment.ALL`. Reasonably different? Then Phase B. Not meaningfully different? Document the decision in an ADR-0029 update and close the item. (Phase B — implement) Switch to `Adjustment.ALL`; re-backfill per the ADR-0029 procedure; spot-check a high-dividend ticker.
**Depends on.** P0.1; Phase B depends on Phase A.
**Effort.** Phase A: ~30 min decision + analysis. Phase B (if yes): ~2 hours + re-backfill window.
**Refs.** ADR-0029 "Consequences" §2; `post_4_9a_cleanup.md`.

---

## Priority 2 — From parked 4.9b: rebalance plumbing on the existing brokers

These are 4.9b items that work fine on the current 2-broker setup and add real user value during a multi-month soak. The household-view items (P2.4, P2.5) are bigger and worth doing as a unit; do P2.1–P2.3 first.

### P2.1 — `target_change_event` audit table

**What.** Add the append-only `target_change_event` table that records every accepted edit (diff JSON, source: `yaml_direct` | `yaml_magic_link` | `admin_endpoint`, confirmed_by).
**Why.** No-cost audit trail. Standalone valuable (you can see what target edits happened when and why). Prerequisite for P2.2 (magic-link guardrails) and any future "evolution of targets" retrospective in the annual review.
**Scope.** (In) the table, an Alembic migration, `services/targets.py` writes a row on every `load_targets_into_db` call. (Out) any UI for browsing the audit; the SQL is enough.
**Depends on.** P0.1.
**Effort.** ~2 hours including migration + test.
**Refs.** `phase_4_9b_guide.md` §6.

### P2.2 — YAML edit guardrails + magic-link confirmation (≥ 10pp shift)

**What.** Pre-commit hook on `data/targets/*.yaml` and `data/household_targets.yaml`: compute per-ticker shift; if `max(|shift|) > 10pp`, hold the commit and send a magic-link email; clicking commits via `GET /admin/targets/confirm?token=<hmac>` and writes a `target_change_event` row.
**Why.** Prevents the "I edited the YAML at midnight and didn't realize I 60-pp-shifted a target" footgun. The magic-link infrastructure already exists from the un-accept path (ADR-0032); reuse the `sign_action` pattern with a distinct namespace (`targets-confirm-v1`).
**Scope.** (In) pre-commit hook, magic-link confirmation flow, threshold setting (`TARGET_EDIT_MAGIC_LINK_THRESHOLD_PCT` default 10). (Out) dashboard editor (Phase 5b); per-user thresholds; magic-link for sub-threshold edits.
**Depends on.** P2.1 (writes `target_change_event` on confirm); ADR-0032 magic-link pattern.
**Effort.** ~4 hours.
**Refs.** `phase_4_9b_guide.md` §5.

### P2.3 — Funds-added detection per broker

**What.** Daily 18:00 ET cron that compares today's `equity_usd` against yesterday's, subtracts market-moves + realised PnL, and flags an unexplained delta > `FUNDS_DETECTION_THRESHOLD_USD` (default $500). Emits a `funds_event` row + an email naming the broker.
**Why.** High user value — you find out by email when funds have moved in or out, with the broker named explicitly. Cross-broker transfers surface as two events with a header note "consider whether these are a single transfer".
**Scope.** (In) `jobs/funds_detection.py`, `funds_event` append-only table, email template using the shared `_components.html.j2`. (Out) automatic categorization of "deposit vs withdrawal vs broker-margin-call"; deep tax-lot tracking; cross-currency complications beyond Tiger's existing AUD-to-USD pattern.
**Depends on.** P0.1.
**Effort.** ~1 day.
**Refs.** `phase_4_9b_guide.md` §3; `post_4_9a_changes.md` §3 (broker_account_id partitioning already in place).

### P2.4 — `household_target_allocation` + `services/household_summary.py`

**What.** Optional `household_target_allocation` table (per-ticker `target_pct` of total household equity); `services/household_summary.py` produces a `HouseholdSnapshot` frozen dataclass aggregating per-broker positions; household drift computation against explicit or implied target.
**Why.** Even at 2 brokers, the "what's my overall exposure to AAPL across Alpaca + Moomoo" question is real. The implied household target (per-broker sum) is meaningful with just 2 brokers; adds value without requiring a 3rd or 4th broker.
**Scope.** (In) table, service, drift logic, ADR (ADR-0034 per the reservation map). (Out) the consolidated email rendering — that's P2.5; ship this first as data infrastructure.
**Depends on.** P0.1; P2.1 (a household-target edit also writes `target_change_event` with `broker_account_id IS NULL`).
**Effort.** ~1 day.
**Refs.** `phase_4_9b_guide.md` §1; per the live ADR sequence, this gets ADR-0034.

### P2.5 — Consolidated daily + weekly summary emails + `email_aggregation` toggle

**What.** Consolidated daily + weekly emails with the household header + per-broker sections; user setting `email_aggregation` controls per-broker / consolidated / both. Default flips to `consolidated` when `household_target_allocation` is configured.
**Why.** Reduces email volume (2 broker emails → 1 consolidated) without losing the per-broker drill-down. Tests the shared email design system (ADR-0031) against the household-view layout — meaningful exercise of the macros.
**Scope.** (In) new `templates/household_daily.*` and `templates/household_weekly.*`, `email_aggregation` setting, email job updates. Must follow ADR-0031's shared `_components.html.j2` discipline. Must include `cancelled` status (ADR-0032) as a distinct funnel line. (Out) the dashboard-editor path (Phase 5b); per-user toggling (single-user).
**Depends on.** P2.4 (data); ADR-0031 (email components); ADR-0032 (cancelled status rendering).
**Effort.** ~2–3 days including email preview + visual verification.
**Refs.** `phase_4_9b_guide.md` §2; ADR-0031; ADR-0032.

---

## Priority 3 — Calendar-driven prompts (4.9b §4)

These are cron-driven rebalance prompts. Each is small. Worth doing once P2.1 (target_change_event) lands so the audit trail is complete.

### P3.1 — Quarterly review cron

**What.** First trading day of Jan / Apr / Jul / Oct at 06:00 ET. Email shows per-broker drift over the quarter + household drift (if P2.4 is in) + any ticker > 15% off-target for > 30 days within the quarter. Links to the YAML files (and eventually to the dashboard in Phase 5b).
**Why.** Forces a quarterly "should I rebalance?" moment without nagging. Bounded; calendar-driven.
**Scope.** (In) `jobs/quarterly_review.py`, market-calendar handling for the "first trading day" (use `pandas_market_calendars`), email template using `_components.html.j2`. (Out) automatic rebalance execution; investment-advice-grade commentary.
**Depends on.** P2.1; ideally P2.4 (richer with household drift) but optional.
**Effort.** ~half day.
**Refs.** `phase_4_9b_guide.md` §4a.

### P3.2 — Annual review cron

**What.** First trading day of January at 06:00 ET. Quarterly-review shape + a year-over-year drift comparison + a count of edits from `target_change_event` for the year. **Approximate-cost-basis caveat included** (sum of fills minus sells) for any below-cost position; not lot-level.
**Why.** Annual ritual. The `target_change_event` audit is what makes "you edited targets N times this year" honest.
**Scope.** (In) `jobs/annual_review.py`. (Out) tax-grade reporting; lot-level tracking; integration with TurboTax / Sharesight (Phase 6+).
**Depends on.** P2.1 (count from `target_change_event`); P3.1 (reuse most of the rendering).
**Effort.** ~half day on top of P3.1.
**Refs.** `phase_4_9b_guide.md` §4b.

### P3.3 — Tax-year-end reminder (jurisdiction-aware)

**What.** A reminder email on the right date for the user's tax jurisdiction. **Jane's jurisdiction is AU** (Tiger TBAU, Moomoo AU) — the Australian tax year ends **30 June**, not 31 December. The 4.9b guide assumed US (Dec 20 for tax-year-end). Implement with `tax_jurisdiction` setting (`AU` | `US`, default `AU` for solo Jane) and gate the email date on it.
**Why.** US-only timing would be wrong for solo Jane. The setting also future-proofs Phase 5 multi-tenant — different users in different jurisdictions.
**Scope.** (In) `tax_jurisdiction` setting; jurisdiction-specific date (AU: ~20 June; US: ~20 December); email body with jurisdiction-specific checklist (AU: super contributions, capital gains discount holding period; US: TLH, IRA contribution deadline). (Out) actual tax filing integration; cross-jurisdiction users.
**Depends on.** P3.2 (reuse the rendering pattern).
**Effort.** ~3 hours including the AU-vs-US setting plumbing and jurisdiction-specific copy.
**Refs.** `phase_4_9b_guide.md` §4c; note the 4.9b guide assumed US — update it when this lands.

---

## Priority 4 — Observability & runbook (from Phase 5 minus the MT parts)

A few Phase 5 items are solo-applicable. The post-WAL-fix soak especially benefits from improved observability — silent failures are exactly what we're trying to surface.

### P4.1 — Structured logs via `structlog`

**What.** Migrate `logging.getLogger(__name__)` to `structlog` with JSON output in production. Every log line carries `request_id` (for API calls), `cron_run_id` (for jobs), `broker_account_id` where applicable, `purpose` for LLM calls.
**Why.** Today's logs are unstructured text; debugging the WAL bug required reading raw stderr. Structured logs make grep-then-jq-then-aggregate a one-line operation. Solo-applicable, MT-extensible.
**Scope.** (In) `structlog` configuration in `main.py`, gradual migration of the noisier modules. (Out) external log aggregation (Datadog, Better Stack) — local JSON to stdout is enough at solo scale.
**Depends on.** P0.1.
**Effort.** ~1 day initial setup + ongoing per-module migration.
**Refs.** `phase_5_guide.md` §5c.3.

### P4.2 — Sentry integration

**What.** Wire Sentry SDK to capture: any uncaught exception, every `WARNING` on a job run, every `broker_error` not classified as `BrokerValidationError`, kill-switch events, attestation-gate trips (when ADR-0027/0030's attestation lands in Phase 5).
**Why.** The WAL bug went unnoticed for months partly because there was no error-aggregation surface. Sentry's free tier covers solo volume easily. When Phase 5 lands, this becomes the foundation of the operational dashboard.
**Scope.** (In) Sentry DSN setting, init in `main.py`, manual `capture_exception` calls in `jobs/*.py` exception handlers. (Out) PagerDuty / on-call alerting (solo); release-tracking integration (do later if useful).
**Depends on.** P0.1; ideally P4.1 (structured logs make Sentry's context richer).
**Effort.** ~half day.
**Refs.** `phase_5_guide.md` §5c.3.

### P4.3 — `operational_runbook.md`

**What.** New top-level file. Covers: backup/restore of `me_invest_dbdata` (P0.3 procedure); CNN scrape failure response (ADR-0030 matrix); OpenD daemon restart; Alpaca/Moomoo API key rotation; Tavily / Anthropic / Finnhub key locations + monthly cost monitoring; the `init_db` fail-fast pragma check's expected response; the structured-log + Sentry conventions (once P4.1/P4.2 land).
**Why.** Today there's no single place that says "if X happens, do Y." The post-WAL-fix audit revealed how easy it is for an operational concern to live only in a commit message. This is a Phase 5 multi-tenant prerequisite that also benefits solo Jane immediately (especially during a multi-month soak).
**Scope.** (In) the new file at top-level (matches `pre_phase_5_manual_testing_checklist.md` pattern). Cross-references the relevant ADRs. (Out) Phase 5 user-facing docs (legal copy, signup flow); customer support runbooks (no customers yet).
**Depends on.** P0.3 (backup procedure); ideally P4.1 and P4.2 too (so the log/Sentry sections are accurate).
**Effort.** ~half day initial + ongoing accretion.
**Refs.** `phase_5_guide.md` §5c.4; `post_4_9a_cleanup.md` "Open — specific implementation concerns".

---

## Priority 5 — Polish / nice-to-have (if time allows)

Don't start these until the higher tiers are clear. Document at the top of any PR which P-tier you skipped to and why.

### P5.1 — Suggestion outcome retrospective in the weekly review

**What.** Weekly digest: "of last week's accepted suggestions, what filled / didn't fill / partial / cancelled". Counts plus a sortable table.
**Why.** Builds the dataset that a future calibration loop (Phase 6+) would consume. Even without the loop, useful for "am I following my own suggestions?" introspection.
**Scope.** (In) query in `services/weekly_review_metrics.py`, render section in the weekly review email using shared components. (Out) any feedback into the LLM scoring; that's calibration-loop territory (deferred).
**Depends on.** P0.1; ADR-0032 (cancelled status).
**Effort.** ~3 hours.
**Refs.** `post_4_9a_changes.md` §11a (the "Orders This Week" daily section is the daily-cadence equivalent; this is the weekly-retrospective version).

### P5.2 — Pre-commit hook: `warn_unused_ignores` enforcement

**What.** Pre-commit hook that runs `uv run mypy src/ --warn-unused-ignores` and fails the commit on any new ignore. Keeps the post-4.9a "mypy 30 → 0" hygiene baseline.
**Why.** The 0-error mypy baseline is easy to slip from. A hook is cheap insurance.
**Scope.** (In) `.pre-commit-config.yaml` addition. (Out) full ruff-or-mypy CI runner setup if pre-commit isn't already configured.
**Depends on.** Pre-commit framework being in use (the 4.9a guide mentions `uv run pre-commit install` — verify).
**Effort.** ~30 minutes.
**Refs.** `post_4_9a_changes.md` §7.

### P5.3 — Sentiment widget "unavailable" UX vs silent-hide

**What.** When VIX/F&G have been NULL for the canary threshold (P1.5), the Market Sentiment widget renders a small "sentiment data temporarily unavailable" message instead of being absent entirely.
**Why.** Silent absence reads as "the market is in a neutral state" — itself a signal a casual reader might misinterpret. Phase 5 users especially won't have the context Jane does. Worth small UX work even at solo scale to prove the pattern.
**Scope.** (In) `_sentiment.html.j2` conditional rendering when canary trips. (Out) any UX research; A/B testing.
**Depends on.** P1.5 (canary signal).
**Effort.** ~1 hour.
**Refs.** ADR-0030; review-feedback in prior session on "silent absence is itself a signal".

---

## Sequencing recommendation

Roughly:

1. **Week 1 of soak window:** P0.1, P0.4, P0.5, P0.6 (all small, all unblocking). Start P0.2 (audit) and P0.3 (backup procedure) in parallel.
2. **Week 2–3:** P0.2, P0.3 finish. P1.1 (ticker names), P1.2 (recon window), P1.5 (CNN canary) ship.
3. **Week 3–4:** P1.3 (manual-cancel gap), P1.4 (prefetch hardening), P1.6 (dividend decision).
4. **Month 2:** P2.1 → P2.2 → P2.3 in sequence. Then P2.4 → P2.5 as the household-view unit.
5. **Month 3:** P3.1 → P3.2 → P3.3 (calendar prompts). P4.1 (structured logs) gradually in the background.
6. **Month 3+:** P4.2 (Sentry), P4.3 (runbook). P5.* items as time allows.

If the soak surfaces any new bugs in the post-4.9a hardening batch during this window, **stop feature work and triage the bug first**. The soak's primary purpose is operational signal; feature work is secondary.

## When the soak ends

When you're satisfied the post-4.9a batch is steady-state (no new bug classes for N consecutive weeks), the natural unparking order is:

1. **4.9c** (IBKR + Tiger adapters) — extends the broker roster on top of the now-hardened 2-broker plumbing. Apply the P0.4 ADR-0033 snapshot-`ts` contract as a precondition.
2. **4.9b** (household + rebalance) — only the pieces *not* already shipped in this soak-window plan (P2.4 + P2.5 deliver household summary; P2.1–P2.3 + P3.* deliver rebalance prompts; the dashboard editor remains parked into Phase 5b).
3. **Phase 5** — multi-tenant productization. With P4.1 (structured logs), P4.2 (Sentry), and P4.3 (runbook) in place, the Phase 5 observability work has a starting point rather than starting from zero.

The `product_plan.md` Phase 4.9b / 4.9c / Phase 5 entries already carry the resume-criteria; this file's purpose is just to use the soak time well rather than waiting passively.

---

## Cross-references

- `post_4_9a_changes.md` — source of the §1–§12 hardening narrative.
- `post_4_9a_cleanup.md` — durable backlog (this plan is a *curated execution order* for a subset of those items).
- `product_plan.md` "Architecture Decision Records" section — ADRs 0024–0032 live here.
- `phase_4_9b_guide.md`, `phase_4_9c_guide.md` — parked guides; the resumption sequencing at the bottom of this file pulls from them.
- `phase_5_guide.md` — parked; P4.1 / P4.2 / P4.3 are the solo-applicable subset.
- `CLAUDE.md` — conventions and gotchas; P0.5 and P0.6 update it.
