# Phase 2 Completion Report

**Project:** Investor Assistant  
**Owner:** Jane  
**Phase:** 2 — Technical Indicators & Weekly Suggestions  
**Code complete:** 2026-05-06  
**Git tag:** deferred until first Sunday suggestions email received in production

---

## 1. Scope vs. delivery

The product plan defined Phase 2 as:

> Technical indicators (SMA, EMA, RSI, MACD) computed from bar Parquet files. Support/resistance levels derived from pivot points, moving averages, and swing highs/lows. Weekly order suggestions generated per the half-the-gap sizing rule and emailed Sunday evenings. Daily email gains a "Levels at a Glance" section. Bar management becomes automatic.

All planned deliverables were met. Two design issues were identified and corrected during the build: the weekly job was initially named `weekly_orders` (contradicting the suggest-only product principle) and was renamed `weekly_suggestions`; bar backfill was initially called unconditionally on every run (expensively refetching 2 years each time) and was rewritten as smart backfill (2-year fetch for new tickers, incremental from last bar date for existing ones). An additional feature — untracked position warnings — was added at review time after finding that broker positions with no `targets.yaml` entry were silently ignored.

---

## 2. What was built

### New endpoints

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/indicators` | GET | — | Latest SMA-20/50/200, EMA-21, RSI-14, MACD per watchlist ticker, computed from bar Parquet files |
| `/suggestions` | GET | — | Pending weekly order suggestions for the current week; returns `[]` before the first Sunday job runs |
| `/admin/run-weekly-suggestions` | POST | ✓ | Manual trigger for the weekly suggestions job (bar sync → indicators → levels → suggestions → email) |

### Updated endpoints

| Endpoint | Change |
|---|---|
| `/admin/run-sync` | Now requires `X-Admin-Token` header (was open in Phase 1) |
| `/admin/run-daily-report` | Now requires `X-Admin-Token` header (was open in Phase 1) |
| `/admin/reload-targets` | Now requires `X-Admin-Token` header (was open in Phase 1) |

### Scheduler

A second CronTrigger was registered alongside the existing daily report trigger:

```
Sunday at 18:00 America/New_York
misfire_grace_time = 21600s (6 h — runs up to 6 h late if box was offline at fire time)
```

### Daily report job changes

Two additions prepended to the existing flow:

1. `update_bars()` — smart bar sync (tolerates failure; stale bars are better than no email)
2. `get_untracked_positions()` — SQL anti-join to find positions with no active target allocation

Result: the daily email now includes a "Levels at a Glance" table and a red "Untracked Positions" warning banner (suppressed when none exist).

### Weekly suggestions job

New job `run_weekly_suggestions`. Order of operations on each Sunday firing:

1. `update_bars()` — bar sync (tolerates failure)
2. `compute_indicators()` — DuckDB window functions + pandas-ta per ticker
3. `take_snapshot()` — live position sync from Alpaca
4. `compute_gap()` — allocation gap vs. targets
5. `compute_levels()` — pivot points, MA-based S/R, fractal swing highs/lows
6. `persist_levels()` — upsert into `sr_level` (idempotent)
7. `build_nearby_levels()` — 3 nearest supports + 3 nearest resistances per ticker
8. `generate_suggestions()` — pure function; distance guard + cash floor guard
9. `persist_suggestions()` — never overwrites non-pending rows
10. `get_untracked_positions()` — surfaces any positions outside target allocation
11. `render_template()` + `emailer.send()` — outside session scope (session-safety rule)

Email subject: `Orders for the week of MMM DD`

### Email template sections

**Weekly suggestions email (new):**

| Section | Content |
|---|---|
| Header | "Orders for the week of MM-DD", equity, cash, broker/mode |
| Untracked positions | Red banner — positions held with no target allocation; prompts to add to `targets.yaml` or trim |
| Suggestions table | Ticker, side, qty, limit price, current price, ~$ cost, reason |
| Top-line summary | # buys ($X to deploy) \| # trims |
| Levels at a glance | Last price, SMA-50/200 with %Δ, RSI-14, nearest support, nearest resistance |
| Footer | "No orders are placed automatically. Suggestions expire Friday at close." |

**Daily report email additions (Phase 2):**

| Section | Added content |
|---|---|
| Untracked positions | Red banner before allocation table — same anti-join logic as weekly email |
| Levels at a glance | After allocation table — SMA-50/200 distance, nearest S/R per ticker |

### Bar management

`update_bars()` in `services/bars.py` is now the single function for both initial backfill and incremental updates. It detects whether a Parquet file exists per ticker and chooses the start date accordingly:

- No file → fetches 2-year history (backfill)
- File exists → fetches from last bar date (incremental)

Called from: lifespan startup, `run_daily_report`, and `run_weekly_suggestions`. First boot is self-sufficient — no manual backfill step required.

### Database schema added

**`sr_level`**

| Column | Type | Description |
|---|---|---|
| `ticker` | varchar | e.g. `VOO` |
| `type` | varchar | `support` or `resistance` |
| `price` | double | Level price |
| `method` | varchar | Computation method (e.g. `pivot_weekly_S1`, `sma_50`, `swing_low_5bar`) |
| `as_of` | date | Computation date |

Unique on `(ticker, method, as_of)` — re-running the job is idempotent.

**`order_suggestion`**

| Column | Type | Description |
|---|---|---|
| `week_of` | date | Monday of the suggestion week |
| `ticker` | varchar | e.g. `VOO` |
| `side` | varchar | `buy` or `sell` |
| `qty` | double | Suggested share quantity |
| `limit_price` | double | Limit price (nearest qualifying S/R level) |
| `reason` | varchar | Human-readable explanation |
| `status` | varchar | `pending` / `accepted` / `rejected` / `expired` |
| `expires_at` | timestamptz | Friday 21:00 ET of the suggestion week |

Unique on `(week_of, ticker, side)` — one suggestion per ticker per direction per week.

---

## 3. New service layer

| File | Role |
|---|---|
| `src/investor/services/analytics.py` | `duckdb_conn()` context manager — creates normalised `price_bar` view over Parquet; maps Alpaca's `symbol`/`timestamp` column names to `ticker`/`date` |
| `src/investor/services/indicators.py` | `IndicatorRow` frozen dataclass + `compute_indicators()` — bulk SMA via DuckDB window functions, per-ticker EMA-21/RSI-14/MACD via pandas-ta |
| `src/investor/services/levels.py` | `SRLevelRow`, `NearbyLevels` + `compute_levels()` / `persist_levels()` / `build_nearby_levels()` |
| `src/investor/services/suggest.py` | `OrderSuggestionRow`, `SizingRule`, `HALF_THE_GAP` + `generate_suggestions()` / `persist_suggestions()` |
| `src/investor/services/bars.py` | `update_bars()` — smart backfill + incremental Parquet append |
| `src/investor/jobs/weekly_suggestions.py` | Sunday 18:00 ET orchestration — full pipeline from bar sync to email |

### `IndicatorRow` (new in Phase 2)

DuckDB and pandas-ta results are immediately converted to a frozen dataclass at the analytics boundary. Templates and jobs only ever see plain Python values.

```python
@dataclass(frozen=True)
class IndicatorRow:
    ticker: str
    as_of: date
    close: float
    sma_20: float | None
    sma_50: float | None
    sma_200: float | None
    ema_21: float | None
    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    pct_from_sma_50: float | None
    pct_from_sma_200: float | None
```

### `NearbyLevels` (new in Phase 2)

```python
@dataclass(frozen=True)
class NearbyLevels:
    ticker: str
    current_price: float
    supports: list[SRLevelRow]     # ≤ 3, nearest first (descending price)
    resistances: list[SRLevelRow]  # ≤ 3, nearest first (ascending price)
```

### `UntrackedPosition` (new in Phase 2)

SQL anti-join result converted immediately to a frozen dataclass before leaving the service layer.

```python
@dataclass(frozen=True)
class UntrackedPosition:
    ticker: str
    qty: float
    market_value: float
    weight_pct: float
```

---

## 4. Architecture decisions made in Phase 2

### ADR-0006 — S/R Methodology ⚠ Pending LLM Review

Three classes of levels are computed in priority order: **pivot points** (fully deterministic, prior week + prior month H/L/C) → **moving averages** (SMA-20/50/200, EMA-21 as dynamic S/R — already computed, contextual) → **swing highs/lows** (fractal, n=5 confirmation bars, last N bars excluded as unconfirmed). All levels stored with `method` column provenance for future reasoning.

**This methodology is a placeholder.** The current implementation selects the nearest S/R level mechanically with no regard for level strength, confluence, volume confirmation, or broader market context. In Phase 3, Claude (with financial reasoning) should evaluate which computed levels are technically significant for each ticker and assign confidence scores. The `method` column already provides the necessary provenance.

### ADR-0007 — Position Sizing ⚠ Pending LLM Review

Default rule: **half the dollar gap** per order (`HALF_THE_GAP = SizingRule(fraction=0.5)`), anchored at the nearest qualifying S/R level. Two safety guards: distance guard (S/R level must be within 8% of current price) and cash floor (remaining cash after purchase must be ≥ $100).

**The choice to anchor at the nearest S/R level is arbitrary at this stage.** "Nearest" does not mean "most meaningful." This logic should be revisited in Phase 3 alongside the S/R methodology review, where LLM-scored levels can be used to prefer high-confidence anchors over merely close ones. `SizingRule` is a dataclass specifically so the fraction can be overridden without touching engine logic.

### Persist-once, never overwrite (`suggest.py`)

`persist_suggestions()` only inserts or updates rows with `status = "pending"`. Once a suggestion reaches `accepted`, `rejected`, or `expired`, it is permanently frozen — re-running the weekly job on the same week cannot overwrite acted-upon rows. This preserves the audit trail referenced in architecture convention #5.

### Untracked positions

Positions held in the broker with no entry in `targets.yaml` are surfaced as a red warning banner in both email templates. The design explicitly avoids silently ignoring such positions (which would mask paper trades, test positions, or legacy holdings) and equally avoids auto-generating target allocations (which would silently commit the user to a sizing the system invented). The warning persists until the user takes one of two deliberate actions: add the ticker to `targets.yaml` with an explicit allocation, or trim the position in the broker.

### Admin token auth

All `/admin/*` endpoints are protected by `X-Admin-Token` validated against `ADMIN_TOKEN` in `.env`. An empty or missing `ADMIN_TOKEN` causes all admin requests to return 401. The `admin_auth` FastAPI dependency uses `# noqa: B008` to suppress the ruff false-positive for `Depends()` in a function default.

---

## 5. `main.py` changes (Phase 2)

**Admin auth dependency** applied to all five `/admin/*` routes:

```python
def admin_auth(
    x_admin_token: str = Header(default=""),  # noqa: B008
    settings: Settings = Depends(_get_settings),  # noqa: B008
) -> None:
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")
```

**Lifespan bar sync** wired in after `load_targets()`:

```python
try:
    _update_bars(
        targets.watchlist, _settings.alpaca_api_key,
        _settings.alpaca_secret_key, bars_dir=_settings.bars_dir,
    )
except Exception as exc:
    logger.warning("Startup bar sync failed; continuing with existing bars: %s", exc)
```

This makes first-boot self-sufficient: no manual `backfill_bars.py` step is required.

---

## 6. Bugs found and fixed during build

### Bug 1 — Weekly job named `weekly_orders`, contradicting suggest-only product principle

**Symptom:** `/admin/run-weekly-orders` endpoint URL and all related log messages referenced "orders," implying the system places trades.  
**Root cause:** Initial naming used "orders" as shorthand without considering the product constraint ("The system never places orders").  
**Fix:** Renamed job file, function, endpoint URL, scheduler job ID, template names, all log messages, and all response strings to `weekly_suggestions`. Three separate replacement passes were required: underscored form (`weekly_orders`), hyphenated form (`weekly-orders`), and title-case phrase form (`Weekly orders`).  
**Regression:** No regression test needed — naming convention is enforced at code review time, not at runtime.

### Bug 2 — Bar backfill called unconditionally on every run (2-year refetch each time)

**Symptom:** `update_bars()` fetched 2 years of history on every daily and weekly job firing, making both jobs unnecessarily slow after the first boot.  
**Root cause:** `backfill_bars.py` logic was ported directly into the job without adapting it to the incremental use case.  
**Fix:** Rewrote `update_bars()` to inspect per-ticker Parquet files: if a file exists, fetch only from the last bar date (`last_date + 1 day`); if no file exists, fetch the full 2-year history. A single bulk Alpaca call fetches all tickers from `min(all start dates)` to avoid N sequential API calls.

### Bug 3 — Positions with no target allocation silently ignored

**Symptom:** A TQQQ paper-trade position held in the broker was invisible to the daily report and the weekly suggestions engine — no warning, no suggestion, no mention.  
**Root cause:** `compute_gap()` joins against `target_allocation`; positions with no target row are dropped by the inner join. No code surfaced the anti-join set.  
**Fix:** Added `get_untracked_positions(session)` (SQL anti-join + frozen dataclass), `src/investor/sql/untracked_positions.sql`, red warning banners in both daily and weekly email templates.

---

## 7. Test coverage

| Test file | Tests | Coverage |
|---|---|---|
| `tests/test_config.py` | 8 | Settings + YAML loader (unchanged from Phase 1) |
| `tests/test_gap.py` | 10 | Gap computation + band_status (unchanged from Phase 1) |
| `tests/test_load_targets.py` | 5 | Hash-based target dedup (unchanged from Phase 1) |
| `tests/test_email.py` | 3 | FakeEmailer + SMTPEmailer (unchanged from Phase 1) |
| `tests/test_daily_report.py` | 3 | DailyReport + session-close regression (unchanged from Phase 1) |
| `tests/test_indicators.py` | 6 | Synthetic Parquet fixtures, frozen dataclass, SMA population with sufficient/insufficient bars |
| `tests/test_levels.py` | 8 | Weekly pivot formulas, swing detection, `build_nearby_levels` ordering and limits, empty-data guards |
| `tests/test_suggest.py` | 11 | In-band skip, buy/sell generation, distance guard, cash floor guard, no-level skip, frozen dataclass, persist insert/update/no-overwrite/no-duplicate |
| `tests/test_integration_alpaca.py` | 1 | Full chain vs. live Alpaca paper account (skips without API keys — unchanged from Phase 1) |

**Total: 58 unit tests** (up from 29 at Phase 1 close) + 1 integration test.

---

## 8. Known issues and limitations

### S/R methodology is a mechanical placeholder

The current implementation selects levels by proximity only. No consideration of level strength, volume at price, confluence zones, or broader market trend. A level 0.5% below the current price may be a weak MA retouch while a stronger pivot sits 4% away. **Do not rely on generated suggestions for real capital until Phase 3 introduces LLM-scored level evaluation.**

### Position sizing is anchored at nearest level regardless of quality

"Nearest" does not mean "most meaningful." `HALF_THE_GAP` with the closest qualifying S/R level is a reasonable default for paper trading but should be replaced in Phase 3 with a rule that considers level confidence scores from the LLM review pass.

### `positions_snapshot` still grows unbounded

Every sync appends rows. At ~1 sync/day × tickers × 252 trading days, the table stays manageable for several years at current scale, but a periodic pruning job should be considered in Phase 3 or 4.

### Weekly suggestions depend on bars being current

If the lifespan bar sync failed (e.g., Alpaca outage on Sunday), the weekly job proceeds with stale bars and logs a warning. Indicators computed from stale bars may produce lagging signals. No alerting mechanism exists to surface this condition to the user other than the server logs.

### No acceptance/rejection UI

`order_suggestion.status` supports `pending` / `accepted` / `rejected` / `expired`, but there is no endpoint or UI to flip a suggestion from `pending` to `accepted` or `rejected`. The status column exists for the audit trail; updates must be made directly in SQLite until a Phase 4 UI is built.

### First Sunday suggestions email pending

As of 2026-05-06, the weekly job has not fired in production. The git tag for Phase 2 is deferred until the first Sunday email is received and reviewed.

---

## 9. Environment and dependencies

- **Python:** 3.12 (unchanged — pinned in Phase 1)
- **Key new runtime deps:** `duckdb>=1.5.2` (already in `pyproject.toml` from Phase 0 planning), `pandas-ta>=0.4.71b0` (per-ticker EMA/RSI/MACD), `pandas>=2.2` (already present via `alpaca-py`)
- **New config keys:** `admin_token: str = ""` and `bars_dir: str = "data/bars"` in `Settings`; `ADMIN_TOKEN=` added to `.env.example` with generation instructions (`openssl rand -hex 32`)
- **Docker:** No Dockerfile changes required — `data/bars/` is already bind-mounted via `docker-compose.yml`
- **Alembic:** One new revision (`209df72f33ca`) creating `sr_level` and `order_suggestion` tables with their UniqueConstraints

---

## 10. Recommended Phase 3 starting point

Based on the product plan and the two ⚠ flags raised in Phase 2:

1. **LLM-scored S/R levels** — pass computed `sr_level` rows for a ticker (with `method`, `price`, and distance-from-current-price) to Claude (Sonnet 4.6) for confidence scoring. Store the score in a new `confidence` column on `sr_level`. ADR-0006 should be updated once the prompt is stable.

2. **LLM-preferred anchor selection** — update `generate_suggestions()` to prefer highest-confidence levels (not merely closest ones) when choosing `limit_price`. ADR-0007 should be updated with the new rule.

3. **News triage** — Haiku 4.5 for "is this headline material?" classification, Sonnet 4.6 for per-ticker summaries. The `anthropic` package is already in `pyproject.toml`.

4. **Suggestion acceptance/rejection endpoint** — `PATCH /suggestions/{id}` to flip status from `pending` to `accepted` or `rejected`; required before trusting the audit trail for real-capital use.

5. **Moomoo adapter** — `brokers/moomoo.py` implementing `BrokerAdapter` against OpenD on `host.docker.internal:11111`. Swap path: update `BROKER` env var, no other code changes required if ADR-0001 is respected.

### Files Phase 3 will primarily touch

| File | Why |
|---|---|
| `src/investor/services/levels.py` | Add `confidence` field to `SRLevelRow`; add LLM-scoring call |
| `src/investor/services/suggest.py` | Prefer high-confidence anchors over nearest |
| `src/investor/models.py` | Add `confidence` column to `SRLevel` |
| `src/investor/main.py` | Add `PATCH /suggestions/{id}` endpoint |
| `src/investor/brokers/moomoo.py` | New adapter |
| `migrations/` | New Alembic revision for `sr_level.confidence` |
| `docs/adr/0006-sr-methodology.md` | Update with LLM-scoring approach |
| `docs/adr/0007-position-sizing.md` | Update with confidence-weighted anchor selection |

---

## 11. Pre-tag cleanup

Before tagging `v0.2.0-phase-2`, the following should be verified and resolved.

| # | Issue | Status |
|---|---|---|
| 1 | First Sunday suggestions email received and reviewed | ✅ Done — 2026-05-11 (triggered manually after fixing /health 500) |
| 2 | `ruff check src/ tests/` — clean | ✅ Done |
| 3 | `mypy src/` — no new errors in Phase 2 files | ✅ Done |
| 4 | Smoke test: 58 unit tests passing | ✅ Done |
| 5 | Smoke test: admin auth returns 200 with token, 401 without | ✅ Done |
| 6 | Smoke test: `/indicators` returns non-null SMAs after bar backfill | ✅ Done |
| 7 | Smoke test: manual weekly trigger produces `order_suggestion` rows | ✅ Done |
| 8 | Smoke test: idempotency — re-running weekly job produces same row count | ✅ Done |
| 9 | ADR-0006 and ADR-0007 written with ⚠ flags for Phase 3 LLM review | ✅ Done |
| 10 | README updated to Phase 2 (endpoints, bar management, weekly email, data models) | ✅ Done |
| 11 | CLAUDE.md updated with Phase 2 service/job files | ✅ Done |
