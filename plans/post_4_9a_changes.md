# Post-4.9a changes (2026-06-03 → 06-18)

Changes landed after `plans/phase_4_9a_completion.md` / `phase_4_9a_post_review_fixes.md`
(which stop at 2026-06-02) and after the per-broker weekly review (already documented in
**ADR-0024** + `README.md`). All are on `main`, deployed, and not yet folded into a phase
guide or ADR. Grouped by area; each entry notes the commit, rationale, key files, and tests.

> Status: all verified (full suite green, ruff + mypy clean at each step) and live on the
> Docker deployment. No tag cut yet.

---

## 1. LLM call-path tuning — `61077a0` (06-03)

Hardened the plumbing shared by movers triage, weekly review, and weekly suggestions
(prompts unchanged). **`anthropic_api`-backend only**; `agent_sdk` accepts the new params as
no-ops (records `temperature=None`, cache tiers `0`).

- **Temperature** is now set explicitly: `0.0` for structured/classification calls (news
  classify/critic/arbitrate, score_levels, context_adjust), `~0.3` for generative prose
  (suggestion `reason_node`, weekly-context synthesis). Previously every call ran at the
  API default (1.0) → run-to-run variance.
- **Transient retry**: bounded retry (≈3 attempts, exponential backoff + jitter) on
  429/5xx/timeout. Schema-validation failures are *not* retried (deterministic). A dropped
  call previously meant a silently missing mover summary or a downgraded anchor.
- **Prompt-cache mechanism + tier accounting**: `cache_control` plumbing + capture of
  `cache_creation_input_tokens` / `cache_read_input_tokens`, costed at Anthropic's
  1.25× (write) / 0.10× (read) of base input. Measurement only — today's system prompts are
  mostly under the cache minimum so it rarely fires; the recorded ratio reveals when it
  starts paying.
- **weekly_context observability hole closed**: `build_weekly_market_context` now persists
  its Sonnet synthesis call to `llm_call_log` (`purpose="weekly_context"`) — previously its
  spend/latency was invisible.
- **Triage critic → Sonnet**: the three news-triage node models are settings knobs
  (`news_classify_model` HAIKU, `news_critic_model` SONNET, `news_arbitrate_model` SONNET) —
  a different model class now judges the Haiku classifier.

Schema: migration `dec2737608db` adds `temperature`, `cache_write_tokens`,
`cache_read_tokens` to `llm_call_log` (cache-hit ratio derived at query time).
Files: `services/llm.py`, `graphs/_nodes.py`, `graphs/news_triage.py`,
`graphs/suggestion_review.py`, `services/llm_levels.py`, `services/weekly_context.py`,
`jobs/weekly_review.py`, `config.py`, `models.py`.

---

## 2. Movers

### 2a. Crypto news symbols + Tavily gap-fill — `c05d581` (06-04)
Movers showed "no material news" for tickers that actually had news. Causes + fixes:
- Crypto symbol format: Alpaca news needs `BTC/USD`, not `BTC` (`_CRYPTO_NEWS_SYMBOLS`,
  `_alpaca_news_symbol`) — `BTC/USD` returns ~92 articles vs ~9 for `BTC`.
- Thin Alpaca coverage for some names → **Tavily web-search gap-fill** when fewer than 3
  structured articles are found (`fetch_tavily_news`, source `"tavily"`).
- Mondays use a 48h news lookback (Fri/weekend news).

**Constraint kept:** Tavily movers articles are display-only and **never persisted** to
`news_event` (ADR-0020). Files: `services/news.py`, `jobs/movers.py`.

### 2b. Direction-aware tiers + weekly fresh-start — `1c38fd6` (06-09)
A whipsawing ticker could move dramatically without alerting (MU fired −10.9% one week,
bounced +9.7% the next, stayed silent). Two bugs in the tiered-threshold logic:
- **Direction-blind latching** — the tier was tracked on `abs(pct)`, so +5% and −5% were
  the same; a sign flip was treated as a same-tier continuation and suppressed. The tier now
  carries **direction** (from the signed `last_pct_change`): a flip starts a fresh tier in
  the new direction; same-direction moves still escalate tier-by-tier (anti-spam preserved).
- **Cross-week state bleed** — the metric is *today vs prior-Friday close*, so the baseline
  rolls forward weekly, but tier state persisted across weeks (last week's trigger suppressed
  this week's move against a different baseline). Tier state now **resets when the ISO
  measurement week changes** (`_iso_week`, ET). No migration — direction from the already-
  signed `last_pct_change`, week from `last_triggered_at`.

Files: `jobs/movers.py`. Tests: direction-flip re-alerts; weekly fresh-start fires;
same-week/same-direction still suppressed (anti-spam guard).

---

## 3. Weekly review — per-broker + correctness (06-06)

Per-broker fan-out itself is documented in **ADR-0024** + `README` (`ce4deb1`, `36769ef`).
The accompanying correctness fixes are not:

- **Metric scoping by `broker_account_id`** — `ad4afd8`. The weekly-review SQL
  (`alloc_drift`, `funnel_counts`, `order_flow`, `per_ticker_breakdown`) and
  `weekly_review_metrics.py` aggregated across *all* accounts, so a ticker targeted in both
  Alpaca + Moomoo produced duplicate drift rows and mixed-equity denominators. Now every
  metric query is scoped to the account.
- **Drift dedup across same-day snapshots** — `a4f9418`. `alloc_drift` picked snapshots by
  `DATE(ts) = MAX(DATE(ts))`, returning *all* rows on the latest date; with several snapshots
  per day (daily report + weekly review + manual sync) the Mon×Fri join fanned out (≈9×).
  Fixed to select the single latest snapshot batch by full `ts`.
- **Drift basis = total equity, not invested-only** — `e5f5a76`. Drift % was
  `market_value / SUM(market_values)` (share of *invested* capital), but `target_pct` is a
  share of *total equity incl. cash*. Account 61 is ~57% cash, so drift was inflated ~2.3×
  and under-target holdings looked over-target (BTC showed 6.4% "over" when it was 2.76%
  "under"). Now uses `positions_snapshot.weight_pct` directly. Also: drift label shows
  "→ unchanged" when `|drift| < 0.005` instead of mislabeling a 0-held ticker "farther".
- **Late GTC fill reconciliation** — `56438b6`. `get_activities` reads
  `get_orders(after=since)`, and Alpaca's `after` filters by *submitted* time, not fill time;
  a GTC/limit order submitted one day and filled the next was permanently missed once the
  window advanced (BTC sug-42 stuck at `accepted_for_routing`). The reconciliation window now
  extends back to the oldest still-open execution. Also scoped `executions_this_week` per
  account.

---

## 4. Emails — Market Sentiment + ETF indicators

### 4a. VIX/F&G box + MA200-for-ETFs — `f77e10d` (06-07)
Both the weekly suggestions and weekly review emails surface market context:
- A **Market Sentiment** box (VIX + CNN Fear & Greed) in the header, rendered only when a
  fresh `weekly_market_context` row carries a VIX or F&G value (silently absent otherwise).
- "Levels at a Glance": **SMA200 / %Δ200 shown for ETFs only** (`index_etf`/`leveraged_etf`);
  a new `EtfTrendRow` + ETF-trend table (vs 200-day MA) in the weekly review.

Files: `jobs/weekly_review.py`, `jobs/weekly_suggestions.py`, the four email templates.

### 4b. Sentiment data fix — CNN headers + VIX — `acd9a5c` (06-07)
VIX and Fear & Greed had **never once populated** (all `weekly_market_context` rows NULL), so
the new box never rendered. Two pre-existing causes:
- CNN's dataviz endpoint returns **HTTP 418 to bot User-Agents** — added a browser header set
  (UA + Accept + Origin/Referer → cnn.com), verified 200.
- Finnhub's free tier serves **no `^VIX`** data (`quote` returns `c=0`). The same CNN
  `graphdata` payload carries the latest VIX under `market_volatility_vix`, so both come from
  one call. `_fetch_fear_greed` → `_fetch_cnn` returning `(score, label, vix)`; Finnhub kept
  only as a VIX fallback. Files: `services/sentiment.py`.

> ⚠️ Operational fragility: the CNN endpoint is an undocumented scrape. If CNN changes
> anti-bot again, VIX/F&G silently go NULL (the box just hides). Watch for `_fetch_cnn`
> warnings in the logs.

---

## 5. Emails — shared component design system — `1aa36cd` (06-07)

Replaced ad-hoc, copy-pasted markup across the four HTML emails with one design system:
- **`templates/_components.html.j2`** — one token palette (all text WCAG-AA on white), one
  type scale, and macros: `header / footer / preheader / section / subsection /
  untracked_box / levels_table / responsive_style`.
- **`templates/_sentiment.html.j2`** — redesigned Market Sentiment widget: two metric cards,
  large navy numerals, value-derived semantic color, a 5-band position strip for the 0–100
  F&G scale, single-card fallback. Descriptor text uses AA-safe colors (vivid accents only on
  borders/strip).
- **Consistency fix:** all four emails now share header/footer/levels/untracked, killing
  drift — e.g. the daily report's Levels table now gates SMA200/%Δ200 to ETFs (via
  `etf_tickers` threaded through `jobs/daily_report.py`), matching weekly suggestions.
- Accessibility (`role="presentation"`, contrast), hierarchy (type scale + preheaders),
  mobile (`@media` rules: stack header, scroll wide tables via `.sscroll`/`.hdr-cell`).
- **Bug fixed:** HTML entities (`&mdash;`) inside Jinja `{{ }}` expressions autoescaped to
  literal `&amp;mdash;` — switched to the actual em-dash character.

Tests: `tests/test_email_templates.py` (daily + movers render, ETF-only MA200, sentiment
pills, autoescape-leak guard) + `tests/test_email_indicators.py`.

> Preview workflow used (see memory `feedback-email-design-preview`): render to HTML in the
> workspace **and** a PNG via Chrome headless, get approval, then deploy.

---

## 6. Bars / S/R correctness — split-adjustment + distance filter — `85053ca` (06-08)

The weekly review surfaced BTC's nearest support as `swing_low_5bar $5.96` while it trades
~$27. Root cause: ticker **`BTC` is the Grayscale Bitcoin Mini Trust ETF**, which did a
**5:1 reverse split (Nov 2024)**. `update_bars` fetched **RAW (unadjusted)** bars, so the
pre-split history sat at 1/5 scale ($4–6) — a phantom regime; the fractal detector flagged
the pre-split low ($5.96) as a swing low, and with current price at the floor of the
post-split range it was the only support below price (77.6% away). Verified vs Alpaca: RAW
$5.62 → SPLIT-adjusted $28.10 (exactly 5.0×).

- `services/bars.py`: `StockBarsRequest` now passes **`adjustment=Adjustment.SPLIT`** (was
  the RAW default). Splits only, not dividends — S/R should reflect prices the market
  actually traded at.
- `services/levels.py`: `build_nearby_levels` gains **`max_distance_pct` (default 0.50)** —
  drops levels >50% from current price as defence-in-depth.

> ⚠️ Operational: switching to SPLIT required a **one-time re-backfill** — delete
> `data/bars/*.parquet`, then `POST /admin/reload-targets` (full re-fetch). Done 06-08 (BTC
> min low $4.4 → $22.1, nearest support now ~$27.80 ≈ 1% away). Any *future* ticker that
> splits is handled automatically from now on.

---

## 7. Type safety — mypy baseline 30 → 0 — `ebc32f5` (06-07)

Strict mypy now passes across all source files (was 30 "baseline" errors). No runtime change —
every fix is a type-level correction, a behavior-preserving coercion, or a provably-true
guard. Headline: made the `LLMClient` Protocol's `call()` **generic on the response schema**
(`type[T] -> tuple[LLMResponse, T | None]`); cast alpaca-py SDK unions at the adapter
boundary; small guards/annotations elsewhere. `warn_unused_ignores` keeps it at zero going
forward. Files: `services/llm.py`, `brokers/alpaca.py`, `services/bars.py`, `main.py`,
`jobs/weekly_suggestions.py`, `services/auto_trade.py`, `services/snapshot.py`, `db.py`,
`services/weekly_context.py`.

---

## 8. Un-accept path + daily order status — `72d0d5c`…`0b48307` (06-09)

Once a suggestion was `accepted` there was no way to pull it back: a working LIVE order
could only be stopped in the broker UI, and auto-trade then **re-placed** it the same week.
This feature adds visibility + a safe un-accept. Design/plan: `plans/unaccept_path_*.md`.

- **New terminal suggestion status `cancelled`** (`pending|accepted|rejected|expired|cancelled`;
  no migration — free String column). Distinct from `rejected` (declined *before* acting).
  Because `auto_trade._fetch_accepted_unexecuted` only selects `accepted`, marking a suggestion
  `cancelled` also **closes the broker-cancelled re-place footgun** (regression-guarded).
- **Shared cancel helper** `services/orders.py::cancel_working_execution` — re-queries the
  broker, then: filled → refuse; partially filled → cancel remainder (filled shares stand,
  recorded by reconciliation); working → cancel + `broker_cancelled`; terminal/cancel-failure →
  leave for reconciliation. The expiry sweep was refactored onto it (gained partial handling).
- **`services/unaccept.py::unaccept_suggestion`** — guard `accepted`, cancel any working order
  via the helper, refuse if fully filled, else mark `cancelled`. Reused by the endpoint + admin.
- **Endpoints** — prefetch-safe two-step: `GET /suggestions/{sid}/unaccept` renders a confirm
  page (no side effect; shows live broker status); `POST …/unaccept` performs it; plus
  `POST /admin/suggestions/{sid}/unaccept`. HMAC via `sign_action(sid,"unaccept",…)`.
- **Daily email "Open & Committed Orders"** — `compose_daily_report` gathers this-week
  `accepted` suggestions + their latest real execution (`CommittedOrderRow`, status label
  Working/Partially filled/Filled/Awaiting placement); the daily template renders the table
  with a signed **Un-accept** link on cancellable rows (the job passes `base_url` + tokens).
- Weekly-review "Suggestions vs Fills" renders `cancelled` in the muted colour.

Files: `services/orders.py`, `services/unaccept.py`, `jobs/suggestion_expiry.py`,
`services/daily_report.py`, `jobs/daily_report.py`, `main.py`, templates
(`daily_report.*`, `unaccept_confirm.html.j2`, `unaccept_result.html.j2`, `weekly_review.html.j2`).
Tests: `test_orders.py`, `test_unaccept.py`, `test_daily_report_committed.py`,
`test_api_unaccept.py`, plus expiry + auto-trade guard additions. 477 passed, ruff/mypy clean.
Live on `main` (rebuilt + restarted 06-09); no migration, no bar re-backfill.

---

## 9. Snapshot one-ts-per-batch — Moomoo drift-table all-zeros — `fffa6dc` (06-13)

Moomoo's weekly-review "Allocation drift (Mon → Fri)" table showed **every Mon%/Fri%/Drift
as 0.0** (Alpaca's was fine). Root cause: `alloc_drift.sql` selects a snapshot batch via
`ts = MAX(ts)` (assuming one `ts` per sync), but the Moomoo adapter called
`datetime.now(UTC)` **per position**, so its 30 snapshot rows had 30 distinct microsecond
timestamps. `ts = MAX(ts)` then matched a **single row** — a non-target ticker (AAPL 0.07%) —
so every target ticker LEFT-JOINed to NULL → 0. Alpaca captures `now()` once, so it was
unaffected.

Fix:
- `services/snapshot.py`: `take_snapshot` tags **every** row in a sync with `account.as_of`
  (one `ts` per batch), not the per-position `p.as_of` — a broker-agnostic guarantee of the
  invariant the drift SQL relies on.
- `brokers/moomoo.py`: `get_positions` captures `now()` once before the loop (mirrors
  `AlpacaAdapter`).
- One-time data backfill: collapsed each historical account-62 sync (each a clean 15-row,
  single-second batch) to one `ts`, so past weeks re-render correctly. Verified: drift now
  reads QQQ 28.9%, BTC 2.8%, etc. (TQQQ 0 is genuine — not held).

Test: `test_snapshot.py` — positions with distinct per-row `as_of` → all written rows share
one `ts`. 478 passed, ruff/mypy clean. Live on `main` (rebuilt + restarted 06-13).

---

## 10. Target loader — pct-within-band validation — `1cec2db` (06-15)

A reload of `data/targets/62.yaml` exposed that `load_targets` only validated the **sum** of
target percentages, not that each pct sits inside its own band. A band that didn't bracket its
target (QQQ `pct: 25` with `band: [26, 34]` — target below the lower edge) loaded fine and made
the holding read perpetually "under band".

Fix:
- `config.py` `load_targets`: raises if not `band_low <= pct <= band_high` for any ticker
  (in addition to the existing sum check).
- `data/targets/62.yaml` (bind-mounted data, not committed): QQQ band corrected to `[21, 29]`.
- Pinned `types-PyYAML` to keep the `import yaml` mypy check clean.

Test: `test_config.py::test_pct_outside_band_raises`. 480 passed, ruff/mypy clean.

## 11. Daily email — Levels → orders recap + allocation donut

### 11a. Replace "Levels at a Glance" with this-week orders recap — `b353d63` (06-17)

S/R levels are weekly-relevant, not daily, so the daily email's levels table is gone. In its
place: an **"Orders This Week"** section — a headline (`N placed · M filled · $ filled notional`)
plus a compact fills table (ticker, side, qty, fill price, time) over real (`dry_run=False`)
`order_execution` rows created since Monday.

- `services/daily_report.py`: `OrdersThisWeek` + `FillRow`; computed in `compose_daily_report`.
- `jobs/daily_report.py`: the daily job no longer computes indicators/levels or `etf_tickers`
  (dead work now); `update_bars` stays (weekly freshness).
- Templates updated (html + txt); removed the obsolete MA200-levels template test.

### 11b. Allocation donut chart (inline CID PNG) — `83515a8` (06-17), **ADR-0025**

A graphically-designed allocation donut (incl. cash) now anchors the daily Allocation section.
Email clients strip `<svg>` and ignore CSS `conic-gradient`, so the pie is rendered server-side
with **Pillow** and embedded as an **inline Content-ID image** (renders in Gmail without "load
images", works offline).

- `services/charts.py` (new): `build_allocation_pie()` (4× supersample → LANCZOS) + `ALLOC_PALETTE`.
- `services/email.py`: `SMTPEmailer` builds `multipart/related` when `inline_images` are given;
  `EmailSender`/`FakeEmailer` gain the optional param. Plain emails unchanged.
- `services/daily_report.py`: `AllocationSlice` + `_build_allocation_slices` (equity = positions +
  cash; top-8 then "Other"; cash last). Donut + HTML legend share one palette (no colour drift).
- `jobs/daily_report.py`: renders the PNG and attaches it; render failure → legend-only, no broken
  image. New runtime dep `pillow>=10.4`.

Tests: `test_charts.py`, `test_email.py` (MIME structure), `test_daily_report_allocation.py`,
`test_email_templates.py`. 497 passed, ruff/mypy clean. Both daily emails (61, 62) sent with the
donut, no warnings.

## 12. SQLite WAL data-loss fix — DELETE journal + named-volume DB — `02b1859`,`d5b8d99` (06-18), **ADR-0026**

**Symptom:** the §10 target reload for account 62 (adding TSLA, fixing QQQ) reported `updated` and
verified present, then days later the DB had **silently reverted** — TSLA gone with no trace (the
June-1 rows were never even closed), while position snapshots kept advancing. TSLA correctly showed
as *untracked* because it was genuinely no longer an active target.

**Root cause:** the DB was in **WAL journal mode** (set historically by langgraph `SqliteSaver`,
never reverted; nothing in the app manages it) on a **macOS Docker bind mount** (`./data`). WAL's
`-shm`/mmap + POSIX locking is unreliable on Docker Desktop's bind-mount virtualisation (SQLite docs:
WAL "does not work on a network filesystem"). Committed transactions accumulated in `-wal` (the main
`.db` lagged it by ~15h; host vs container even reported different sizes for the same file) and the
un-checkpointed tail was dropped on a restart, reverting to the last checkpoint.

**Fix (both layers):**
- `db.py`: a `connect` event listener forces `PRAGMA journal_mode=DELETE` + `synchronous=FULL` on
  every connection (single-writer app — WAL buys nothing); `init_db` **fails fast** if still WAL.
- `docker-compose.yml`: the OLTP db moved off the bind mount onto a **named volume**
  (`dbdata:/app/db`, `SQLITE_PATH=/app/db/investor.db`). Parquet/DuckDB stay on `./data`.
- `Dockerfile`: `mkdir -p /app/db` as appuser so a fresh empty volume is writable (else
  "attempt to write a readonly database" crash loop).
- One-time migration: checkpoint+convert the live db off WAL, copy into the named volume (chown 1000),
  cutover. Verified: `journal_mode=delete`, `synchronous=FULL`, 13 targets incl. TSLA, 905 position
  rows intact, write path re-confirmed (snapshot 905→919). Old bind-mount db retired (backup
  `investor.db.bak-pre-vol-2026-06-18` kept).

Test: `test_db.py::test_pragmas_force_delete_journal_not_wal`. 498 passed, ruff/mypy clean.

> **Operational note:** the live DB is no longer at `./data/investor.db` — it lives in the
> `me_invest_dbdata` volume. Inspect via `docker compose exec app …` against `/app/db/investor.db`;
> back up with `docker run --rm -v me_invest_dbdata:/db -v "$PWD":/out alpine cp /db/investor.db /out/`.

---

## Candidate ADRs / gotchas (not yet written)

Worth promoting into `docs/adr/` or CLAUDE.md "Common gotchas" if these stick:
- **Bars are split-adjusted (`Adjustment.SPLIT`)** + re-backfill procedure (§6).
- **VIX/F&G come from CNN `graphdata` with browser headers**; Finnhub is a VIX fallback only;
  scrape is fragile (§4b).
- **Emails share `_components.html.j2` + `_sentiment.html.j2`**; never reintroduce per-template
  markup; entities-in-`{{ }}` autoescape trap (§5).
- **Movers tiers are direction-aware and reset per ISO week** (§2b).
- **One sync = one snapshot `ts`** (`take_snapshot` uses `account.as_of` for all rows);
  `alloc_drift` and other batch logic rely on it — adapters must not set per-row `as_of` (§9).
- **Suggestion status `cancelled` is terminal** (un-accept); auto-trade ignores it (§8).
- **`targets.yaml`: each `pct` must lie within its own `band`** — `load_targets` enforces it (§10).
- *(now written)* **SQLite is DELETE-journal, never WAL; OLTP db on the `me_invest_dbdata`
  named volume, not `./data`** — ADR-0026 (§12).
- *(now written)* **Daily email: levels removed (weekly-only) → orders recap + allocation
  donut (inline CID PNG via Pillow; emailer supports `multipart/related`)** — ADR-0025 (§11).

## Still open / parked

Nothing. The 06-09 order-lifecycle gaps (no un-accept path; auto-trade re-placing a
broker-cancelled order) are both resolved by §8.
