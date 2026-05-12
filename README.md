# Investor Assistant — Phase 3a

A self-hosted, **suggest-only** portfolio assistant for long-term US-equity investors. Pulls positions from Alpaca, compares them against a YAML-defined target allocation, computes technical indicators and support/resistance levels, scores levels with Claude Sonnet 4.6, suggests weekly limit orders, and sends daily and weekly reports by email. The weekly email includes Accept/Reject buttons for each suggestion. The system never places orders — execution is always manual in the broker's UI.

**Current phase:** 3a — LLM Level Scoring + Accept/Reject Workflow  
**Status:** Code complete.

---

## Quick start

### Prerequisites

- Python 3.12 + [uv](https://docs.astral.sh/uv/)
- Docker Desktop (for containerised run)
- An [Alpaca](https://alpaca.markets) paper account (free)
- A Gmail account with an [App Password](https://myaccount.google.com/apppasswords) enabled

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env — set ALPACA_*, SMTP_*, ADMIN_TOKEN, ANTHROPIC_API_KEY, and MAGIC_LINK_SECRET
```

Required variables (see `.env.example` for the full list):

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `SQLITE_PATH` | Path to SQLite file, e.g. `data/investor.db` |
| `TARGETS_PATH` | Path to YAML targets, e.g. `config/targets.yaml` |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | e.g. `587` |
| `SMTP_USER` | Gmail address |
| `SMTP_APP_PASSWORD` | Gmail App Password |
| `EMAIL_FROM` | Sender address |
| `EMAIL_TO` | Recipient address |
| `ADMIN_TOKEN` | Token for `/admin/*` endpoints (`openssl rand -hex 32`) |
| `ANTHROPIC_API_KEY` | Anthropic API key for Sonnet 4.6 level scoring |
| `MAGIC_LINK_SECRET` | HMAC signing key for email buttons (`openssl rand -hex 32`, distinct from `ADMIN_TOKEN`) |
| `APP_BASE_URL` | Public base URL for magic links, e.g. `http://localhost:8000` |

### 2. Configure target allocation

Edit `config/targets.yaml`. Percentages must sum to `100 - cash_buffer_pct`:

```yaml
watchlist: [VOO, QQQ, SCHD, AAPL, MSFT, AMZN]
targets:
  VOO:  { pct: 40, band: [35, 45] }
  QQQ:  { pct: 25, band: [21, 29] }
  SCHD: { pct: 15, band: [12, 18] }
  AMZN: { pct: 5,  band: [3,  8]  }
  AAPL: { pct: 5,  band: [3,  8]  }
  MSFT: { pct: 5,  band: [3,  8]  }
cash_buffer_pct: 5
```

---

## Running locally (no Docker)

```bash
# Install dependencies
uv sync

# Apply DB migrations
uv run alembic upgrade head

# Seed target allocation into the database
uv run python scripts/load_targets.py

# Pull current positions from Alpaca
uv run python scripts/sync_positions.py

# Start the API server
uv run uvicorn src.investor.main:app --reload --port 8000
```

The API is available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

The scheduler starts automatically with the server and fires:
- **Daily report**: Mon–Fri at 16:15 America/New_York (30-minute misfire grace)
- **Weekly suggestions**: Sunday at 18:00 America/New_York (6-hour misfire grace)

### Updating targets

1. Edit `config/targets.yaml`
2. Call `POST /admin/reload-targets`:
   ```bash
   curl -H "X-Admin-Token: $ADMIN_TOKEN" -X POST localhost:8000/admin/reload-targets
   # → {"status":"ok","result":"updated"}
   ```

---

## Running with Docker

```bash
# First start
docker compose up --build -d

# View logs
docker compose logs -f app

# Rebuild after code changes
docker compose up --build -d

# Stop
docker compose down
```

The SQLite file and bar Parquet files are stored in `./data/` (bind-mounted, persists across restarts).

---

## API endpoints

### `GET /health`

Returns service status, broker, last sync timestamp, and number of active targets.

### `GET /positions`

Latest snapshot per ticker, ordered by portfolio weight descending.

### `GET /gap`

Allocation gap between current holdings and targets. Positive `gap_pct` = underweight (buy); negative = overweight (trim). `band_status` is `"under"`, `"in_band"`, or `"over"`.

### `GET /drift`

Same as `/gap` but returns only tickers outside their rebalance band.

### `GET /indicators`

Latest technical indicators (SMA-20/50/200, EMA-21, RSI-14, MACD) for all watchlist tickers, computed from the bar Parquet files.

```json
[
  {
    "ticker": "VOO",
    "as_of": "2026-05-02",
    "close": 495.10,
    "sma_50": 490.20,
    "sma_200": 475.30,
    "rsi_14": 58.2,
    "pct_from_sma_50": 0.99,
    "pct_from_sma_200": 4.16
  }
]
```

### `GET /suggestions`

Pending weekly order suggestions for the current week. Returns `[]` before the first weekly suggestions job runs.

```json
[
  {
    "ticker": "VOO",
    "side": "buy",
    "qty": 2.0,
    "limit_price": 488.50,
    "reason": "underweight +8.2% — buy at sma_50 $488.50 (conf 0.78), Tested twice as support in 30 days. closes ~50% of gap",
    "status": "pending",
    "expires_at": "2026-05-08T21:00:00-04:00"
  }
]
```

### `PATCH /suggestions/{id}` *(requires X-Admin-Token)*

Accept or reject a suggestion programmatically.

```bash
curl -X PATCH localhost:8000/suggestions/42 \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "accept", "note": "placed manually in Alpaca"}'
# → {"status": "ok", "id": 42, "new_status": "accepted"}
```

Returns 409 if the suggestion is no longer pending. `action` must be `"accept"` or `"reject"`.

### `GET /suggestions/{id}/{action}?token=...`

Magic-link endpoint hit when the user clicks Accept or Reject in the weekly email. The token is HMAC-signed and expires after 7 days. Returns an HTML confirmation page. Returns 400 on bad/expired token, 409 if already acted.

### `POST /admin/run-sync` *(requires X-Admin-Token)*

Triggers an immediate position sync from the broker.

### `POST /admin/run-daily-report` *(requires X-Admin-Token)*

Manually triggers the full daily report job.

### `POST /admin/reload-targets` *(requires X-Admin-Token)*

Reloads target allocations from `config/targets.yaml`. Idempotent — skips DB write if the file hash is unchanged.

### `POST /admin/run-weekly-suggestions` *(requires X-Admin-Token)*

Manually triggers the weekly suggestions job (indicators → levels → suggestions → email).

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" -X POST localhost:8000/admin/run-weekly-suggestions
```

Interactive docs: `http://localhost:8000/docs`

---

## Daily email report

Fires Mon–Fri at 16:15 America/New_York. Contains:

| Section | Content |
|---|---|
| Header | Date, equity, cash, broker/mode |
| Drift alerts | Yellow banner — tickers outside their rebalance band |
| Untracked positions | Red banner — positions held with no target allocation; prompts to add to `targets.yaml` or trim |
| Allocation table | Ticker, qty, market value, current %, target %, gap %, band status |
| Gap summary | Top 3 underweight + top 3 overweight |
| Levels at a glance | SMA-50/200 distance, nearest support and resistance per ticker |
| Footer | "No orders are placed automatically." |

Subject: `Portfolio — YYYY-MM-DD (equity $XX,XXX)`

---

## Weekly suggestions email

Fires Sunday at 18:00 America/New_York. Contains:

| Section | Content |
|---|---|
| Header | Week of MM-DD, equity, deployable cash |
| Untracked positions | Red banner — same as daily report; persists until resolved |
| Suggestions table | Ticker, side, qty, limit price, current price, distance to level, reason, **Accept / Reject buttons** |
| Top-line summary | Total $ to deploy, # buys, # sells |
| Levels at a glance | SMA-50/200 distance, nearest support and resistance per watchlist ticker |
| Footer | Reminder that execution is manual |

Subject: `Orders for the week of MMM DD`

Each suggestion row has **Accept** (green) and **Reject** (grey) buttons. Clicking one updates the suggestion status directly via an HMAC-signed magic link — no login required. Links expire after 7 days; second click returns a "already acted" message.

Suggestions use "half-the-gap" sizing: each order deploys half the dollar shortfall (or surplus). The limit price is chosen by `select_anchor()`: Claude Sonnet 4.6 scores all computed S/R levels for confidence, and the highest-confidence level within 8% of the current price is used (buy orders use support levels; sell orders use resistance levels). If LLM scoring fails, the system falls back to nearest-distance selection. See [ADR-0006](docs/adr/0006-sr-methodology.md) and [ADR-0007](docs/adr/0007-position-sizing.md) for the full methodology.

---

## Bar data (OHLCV)

Daily OHLCV bars are stored as Parquet files under `data/bars/` and queried by DuckDB for indicators and S/R level computation.

**Bars are managed automatically.** On every server startup the app calls `update_bars()`:
- If a ticker has no Parquet file → fetches the full 2-year history (backfill)
- If a ticker has an existing file → fetches only from the last bar date (incremental)

This means bars are always up to date after a restart, and the first boot takes care of the initial backfill with no manual step required.

The daily and weekly jobs also call `update_bars()` during their run, so bars stay current even between restarts.

To force a manual sync (e.g. after adding tickers to `targets.yaml`):

```bash
uv run python scripts/backfill_bars.py
```

Files are written to `data/bars/<TICKER>.parquet`. The `data/bars/` directory is bind-mounted in Docker and gitignored.

---

## Untracked positions

Any position held in the broker that has **no entry in `targets.yaml`** is flagged as untracked. It appears as a red warning banner in both the daily report and the weekly suggestions email, showing ticker, qty, market value, and portfolio weight.

The banner suggests two resolutions:
1. **Add to `targets.yaml`** — give the position a target allocation and band so the system can manage it normally.
2. **Trim in the broker** — close or reduce the position manually if it was unintentional (e.g. a paper trade, a test position, or a legacy holding you no longer want).

The warning persists in every email until one of those actions is taken.

---

## Data models

All transactional tables are in `data/investor.db` (SQLite).

### `target_allocation` / `broker_account` / `positions_snapshot` / `meta`

See Phase 1 for full column docs. These tables are unchanged in Phase 2.

### `sr_level` (Phase 2+)

One row per ticker × method × as_of date. Methods include pivot points (`pivot_weekly_S1`, `pivot_monthly_R1`, …), moving averages (`sma_50`, `ema_21`, …), and swing levels (`swing_high_5bar`, `swing_low_5bar`). Unique on `(ticker, method, as_of)` — re-running the job is idempotent.

| Column | Type | Description |
|---|---|---|
| `ticker` | varchar | e.g. `VOO` |
| `type` | varchar | `support` or `resistance` |
| `price` | double | Level price |
| `method` | varchar | Computation method |
| `as_of` | date | Computation date |
| `confidence` | float | LLM confidence score [0.0, 1.0] (Phase 3a) |
| `llm_rationale` | text | LLM rationale, truncated to 240 chars (Phase 3a) |
| `scored_at` | timestamptz | When the confidence score was assigned (Phase 3a) |
| `scored_by_model` | varchar | Model that scored this level, e.g. `claude-sonnet-4-6` (Phase 3a) |
| `prompt_version` | varchar | Prompt version used, e.g. `v1` (Phase 3a) |

### `order_suggestion` (Phase 2+)

One row per week × ticker × side. Status lifecycle: `pending` → `accepted` / `rejected` / `expired`. Rows with non-pending status are never overwritten on re-run.

| Column | Type | Description |
|---|---|---|
| `week_of` | date | Monday of the suggestion week |
| `ticker` | varchar | e.g. `VOO` |
| `side` | varchar | `buy` or `sell` |
| `qty` | double | Suggested share quantity |
| `limit_price` | double | Limit price (confidence-weighted S/R level) |
| `reason` | varchar | Human-readable explanation |
| `status` | varchar | `pending` / `accepted` / `rejected` / `expired` |
| `expires_at` | timestamptz | Friday 21:00 ET of the suggestion week |
| `confidence_at_creation` | float | Anchor level confidence when suggestion was created (Phase 3a) |
| `acted_at` | timestamptz | When accept/reject was recorded (Phase 3a) |
| `note` | text | Optional note from the accept/reject action (Phase 3a) |

### `llm_call_log` (Phase 3a)

One row per LLM API call. Used for cost tracking and debugging.

| Column | Type | Description |
|---|---|---|
| `ts` | timestamptz | Call timestamp (UTC) |
| `purpose` | varchar | e.g. `score_levels` |
| `model` | varchar | e.g. `claude-sonnet-4-6` |
| `prompt_hash` | varchar | First 12 hex chars of SHA-256 of system+user prompt |
| `input_tokens` | int | Prompt tokens consumed |
| `output_tokens` | int | Completion tokens produced |
| `cost_usd` | float | Estimated USD cost |
| `latency_ms` | int | Wall-clock latency |
| `status` | varchar | `ok` / `schema_error` / `api_error` |
| `error` | text | Error message if status ≠ `ok` |

---

## Inspecting the database

```bash
sqlite3 data/investor.db
```

```sql
-- pending suggestions for this week
SELECT ticker, side, qty, limit_price, confidence_at_creation, reason
FROM order_suggestion
WHERE week_of = date('now', 'weekday 1', '-7 days') AND status = 'pending';

-- current S/R levels with LLM confidence
SELECT ticker, type, method, price, round(confidence, 2) AS conf, llm_rationale
FROM sr_level WHERE as_of = (SELECT MAX(as_of) FROM sr_level)
ORDER BY ticker, type, price;

-- all-time suggestion history
SELECT week_of, ticker, side, qty, limit_price, confidence_at_creation, status, acted_at
FROM order_suggestion ORDER BY week_of DESC, ticker;

-- LLM cost summary by day
SELECT date(ts) AS day, sum(cost_usd) AS total_usd, count(*) AS calls
FROM llm_call_log GROUP BY 1 ORDER BY 1 DESC;
```

---

## Project layout

```
src/investor/
  main.py             FastAPI app + lifespan
  config.py           pydantic-settings + targets.yaml loader
  db.py               SQLite engine + session factory
  models.py           SQLAlchemy ORM models (Phase 2: SRLevel, OrderSuggestion)
  scheduler.py        APScheduler bootstrap
  brokers/
    base.py           BrokerAdapter Protocol + dataclasses
    alpaca.py         AlpacaAdapter
  prompts/
    score_levels_v1.txt  Sonnet 4.6 scoring prompt (hard rules: no invented prices, no trade recs)
  services/
    snapshot.py       Position + account ingestion
    gap.py            Gap computation + UntrackedPosition detection
    analytics.py      DuckDB context manager (price_bar view over Parquet)
    indicators.py     IndicatorRow + compute_indicators() — SMA/EMA/RSI/MACD
    levels.py         SRLevelRow, NearbyLevels + compute_levels() / persist_levels()
    llm.py            LLMClient (cost guard, schema validation) + persist_llm_call_log()
    llm_levels.py     ScoredLevel + score_levels_for_ticker() via Sonnet 4.6
    magic_link.py     sign_action() / verify_action() — HMAC-SHA256 email tokens
    suggest.py        OrderSuggestionRow, select_anchor() + generate_suggestions() / persist_suggestions()
    daily_report.py   DailyReport dataclass + compose_daily_report()
    bars.py           update_bars() — smart backfill + incremental Parquet append
    targets.py        Hash-based idempotent target loader
    render.py         Jinja2 template rendering
    email.py          SMTPEmailer + FakeEmailer
  jobs/
    daily_report.py   Mon-Fri 16:15 ET — sync, indicators, compose, email
    weekly_suggestions.py  Sun 18:00 ET — indicators, levels, suggestions, email
config/
  targets.yaml        Target allocation (hand-edited)
templates/
  daily_report.html.j2    Daily HTML email
  daily_report.txt.j2     Daily plain-text email
  weekly_suggestions.html.j2   Weekly suggestions HTML email
  weekly_suggestions.txt.j2    Weekly suggestions plain-text email
scripts/
  load_targets.py     Seed/update target_allocation from targets.yaml
  sync_positions.py   One-shot position sync
  backfill_bars.py    Force bar sync for all watchlist tickers (delegates to services/bars.py)
sql/
  gap_allocation.sql          Gap vs target per ticker
  positions_latest.sql        Latest snapshot per ticker
  untracked_positions.sql     Positions with no active target allocation
  account_last_sync.sql       Current account state
  targets_active_count.sql    Count of active targets
migrations/           Alembic revisions
data/
  investor.db         SQLite — bind-mounted, gitignored
  bars/               Parquet bar files — bind-mounted, gitignored
tests/
  test_config.py              Settings + YAML loader (8 tests)
  test_gap.py                 Gap computation + band_status, cash-buffer invariant (11 tests)
  test_load_targets.py        Hash-based target dedup (5 tests)
  test_email.py               FakeEmailer + SMTPEmailer (3 tests)
  test_daily_report.py        DailyReport + session-close regression (3 tests)
  test_indicators.py          compute_indicators() with synthetic Parquet (6 tests)
  test_levels.py              Pivot formulas, swing detection, build_nearby_levels (8 tests)
  test_llm.py                 LLMClient cost guard, schema validation, _calc_cost (11 tests)
  test_magic_link.py          sign_action / verify_action — format, tamper, expiry (12 tests)
  test_suggest.py             generate_suggestions, select_anchor, persist lifecycle (27 tests)
  test_integration_alpaca.py  Full chain vs live Alpaca paper (1 test, skips without keys)
docs/adr/
  0001-broker-adapter-abstraction.md
  0002-three-tier-storage-architecture.md
  0003-schema-migrations-alembic-sqlite.md
  0004-bar-storage.md
  0005-email-failure-policy.md
  0006-sr-methodology.md      ⚠ Pending Phase 3c (scoring pass partial update added)
  0007-position-sizing.md     ⚠ Pending Phase 3c (confidence-weighted anchor added)
  0009-llm-guardrails.md      Hard rules for LLM output in the suggestion pipeline
  0010-magic-link-auth.md     HMAC magic-link auth for Accept/Reject email buttons
```

---

## Development

```bash
uv sync
uv run pytest                        # 104 unit tests + 1 integration (skipped without API keys)
uv run pytest -m "not integration"   # unit tests only
uv run ruff check --fix
uv run mypy src/
```
