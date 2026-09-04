# Investor Assistant — Phase 4.9a (paper-only public build)

> ⚠️ **Paper trading only.** This build cannot connect to a live brokerage
> account. A four-layer invariant in [`src/investor/safety.py`](src/investor/safety.py)
> blocks live trading at the adapter, the config, both factories, and the
> order-submission chokepoint. See [ADR-0036](docs/adr/0036-paper-only-public-build.md).
> Nothing here is financial advice.

📈 **Just want to see what it actually did?** → **[14 weeks of weekly outcomes](docs/weekly-outcome/)** — two charts per week from the paper account (equity, allocation vs target, suggestion funnel), with a captioned index. It includes the weeks that filled nothing at all, and the best week of the series, which had no trades in it.

A self-hosted portfolio assistant for long-term US-equity investors. Pulls positions from Alpaca, compares them against a YAML-defined target allocation, computes technical indicators and support/resistance levels, scores levels with Claude Sonnet 4.6, and suggests weekly limit orders with 2–4 sentence analyst-style rationales. Before suggestions reach your inbox, a LangGraph review pipeline runs: Sonnet writes a per-draft rationale, a **context-adjust node** applies a deterministic earnings gate (Finnhub) and a bounded Sonnet narrative multiplier from Friday's persisted market context, a critic pass reviews all drafts as a set, and deterministic Python applies any changes the critic proposes. When a watchlist ticker moves ≥5% vs. last week, a movers email fires with AI-triaged news. Every Friday a **weekly review email** covers realised PnL, suggestion outcomes, an **Order Activity summary** (funnel counts, dollar flow, allocation drift Mon→Fri, per-ticker breakdown, 4-week trend), auto-trade status, and a Tavily-powered market context narrative — which is also **persisted to the database** so Sunday's sizing can be informed by Friday's macro narrative.

**By default the system is suggest-only** — execution is always manual in the broker's UI. Phase 4 adds an opt-in **auto-trade mode** (off by default, three-state `OFF` / `DRY_RUN` / `LIVE`, gated behind a promotion token, hard spending caps, and a kill switch) that places already-accepted suggestions through the broker API. After each broker fill, the **reconciliation engine** matches fills back to suggestions, computes FIFO realised PnL, and flags unmatched manual trades for review.

**Current phase:** this is the **paper-only public build** of a private multi-broker system — see [ADR-0036](docs/adr/0036-paper-only-public-build.md). The private build supports a second live broker (Moomoo) and real order execution; this build ships **Alpaca paper only**, enforced by the four-layer invariant in `src/investor/safety.py`. The multi-broker data model, jobs, and API scoping (ADR-0024) are retained unchanged — connecting a second broker account still works end-to-end, it just can never be a live one.  
**Status:** the app is fully multi-broker across the data model, jobs, scheduler, and API; the post-4.9a hardening batch (ADR-0025–0033) plus soak-window P0/P1/P2-Wave-A work (backup job, integrity audit, funds detection, target-change audit) are live. See `plans/soak_window_work_report.md` for the running completion record. Phase 4.9b (household view), 4.9c (IBKR + Tiger), and Phase 5 (multi-tenant) are parked pending the soak.
>
> The **foundation-hardening commit** (env.py + f2680 fixes, the `adopt_legacy_create_all_tables` migration, and the `init_db` reorder making Alembic the single source of truth) carries **no** auto-trade gate — it is a no-op on the existing DB and is safe to deploy independently.

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
# Edit .env — set ALPACA_*, SMTP_*, ADMIN_TOKEN, ANTHROPIC_API_KEY, MAGIC_LINK_SECRET, FINNHUB_API_KEY
```

Required variables (see `.env.example` for the full list):

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `SQLITE_PATH` | Path to SQLite file (local: `data/investor.db`; Docker overrides this to `/app/db/investor.db` on a named volume — ADR-0026) |
| `TARGETS_PATH` | Path to YAML targets, e.g. `config/targets.yaml` |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | e.g. `587` |
| `SMTP_USER` | Gmail address |
| `SMTP_APP_PASSWORD` | Gmail App Password |
| `EMAIL_FROM` | Sender address |
| `EMAIL_TO` | Recipient address |
| `ADMIN_TOKEN` | Token for `/admin/*` endpoints (`openssl rand -hex 32`) |
| `ANTHROPIC_API_KEY` | Anthropic API key for Haiku + Sonnet news triage and level scoring |
| `MAGIC_LINK_SECRET` | HMAC signing key for email buttons (`openssl rand -hex 32`, distinct from `ADMIN_TOKEN`) |
| `APP_BASE_URL` | Public base URL for magic links, e.g. `http://localhost:8000` |
| `FINNHUB_API_KEY` | Finnhub API key — news fallback AND Phase 4.7 earnings gate ([free tier](https://finnhub.io), 60 req/min); empty = earnings gate is a no-op |
| `LLM_DAILY_COST_CAP_USD` | Daily LLM spend cap in USD (default `3.0`) |
| `REFLECTION_ENABLED` | Opt-in weekly reflection section in the Friday email (default `false`). Costs one Sonnet call per broker account per week; its insights feed only the next reflection and never reach the suggestion engine |
| `LLM_BACKEND` | `anthropic_api` (default) or `agent_sdk` (routes calls through `claude-agent-sdk`) |
| `LLM_CLI_PATH` | Path to system `claude` CLI for `agent_sdk` backend; empty = use SDK-bundled binary |
| `AUTO_TRADE_PROMOTION_TOKEN` | Separate token required for auto-trade mode promotions (`openssl rand -hex 32`) |
| `TAVILY_API_KEY` | Tavily search API key ([free tier](https://tavily.com), 1 000 searches/month); empty = weekly market context section omitted |
| `TAVILY_MONTHLY_CAP` | Monthly search cap (default `200`; prevents accidental overuse) |
| `EARNINGS_SIZE_FACTOR` | Qty multiplier for tickers with earnings this week (default `0.5`; Phase 4.7) |
| `EARNINGS_REANCHOR` | Move limit price to a deeper S/R level when earnings are near (default `true`; Phase 4.7) |
| `EARNINGS_LOOKAHEAD_DAYS` | Days forward from week_of to check for earnings (default `7`; Phase 4.7) |
| `CONTEXT_SIZE_MIN` | Lower clamp on narrative size multiplier (default `0.25`; Phase 4.7) |
| `CONTEXT_SIZE_MAX` | Upper clamp on narrative size multiplier (default `1.5`; Phase 4.7) |
| `CONTEXT_MAX_AGE_DAYS` | Max age of persisted Friday context for Sunday to use it (default `4`; Phase 4.7) |
| `CONTEXT_ADJUST_PROMPT_VERSION` | Context-adjust prompt version (default `v1`; Phase 4.7) |
| `CRITIC_PROMPT_VERSION` | Suggestion critic prompt version (default `v2`; Phase 4.7) |

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

| Time (ET) | Days | Job |
|---|---|---|
| 09:00 | Mon–Fri | **Suggestion expiry sweep** — cancels stale GTC orders at the broker and marks suggestions `expired` (runs before auto-trade to prevent duplicate orders) |
| 09:35 | Mon–Fri | **Auto-trade pass** — place orders for `accepted` suggestions (mode-gated; default OFF) |
| 16:15 | Mon–Fri | **Daily report** — sync, indicators, compose, email |
| 16:30 | Mon–Fri | **Movers email** — threshold crossings + AI-triaged news |
| 16:45 | Mon–Fri | **Daily reconciliation** — match broker fills to suggestions, FIFO PnL; polls broker for broker-cancelled executions |
| 17:00 | Friday | **Weekly review email** — suggestion outcomes, Order Activity summary (funnel/flow/drift), auto-trade status, market context narrative; persists context to DB for Sunday sizing |
| 18:00 | Sunday | **Weekly suggestions** — indicators, levels, LLM scoring, review graph, email |

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

Bar Parquet files and the DuckDB analytics file are stored in `./data/` (bind-mounted). The **SQLite OLTP database lives on a Docker named volume** (`dbdata` → `/app/db/investor.db`), *not* the bind mount — SQLite journaling is unreliable on Docker Desktop bind mounts and silently lost a write once (see [ADR-0026](docs/adr/0026-sqlite-journaling-and-db-volume.md)). `docker-compose.yml` overrides `SQLITE_PATH` to `/app/db/investor.db` for this reason. Both persist across restarts.

Back up the database with:

```bash
docker run --rm -v me_invest_dbdata:/db -v "$PWD":/out alpine cp /db/investor.db /out/investor.db.bak
```

---

## API endpoints

### `GET /health`

Returns service status and a per-broker-account summary: for each active account, its `broker_account_id`, nickname, broker, `auto_trade_mode`, last sync timestamp, and active target count.

### Broker accounts (Phase 4.9a)

- `POST /admin/broker-accounts` *(X-Admin-Token)* — onboard a broker. Body `{"broker": "alpaca", "nickname": "Long-term", "connection_config": {...}}`. Builds the adapter (400 on bad config), creates the identity row with a fresh `account_ref`, seeds `auto_trade_state` at `OFF`, and registers the adapter live (no restart). Returns the new `broker_account_id`. `connection_config.paper` is deliberately ignored — every adapter this endpoint can build is forced to `paper=True` (L2 of the paper-only invariant, [ADR-0036](docs/adr/0036-paper-only-public-build.md)).
- `GET /admin/broker-accounts` *(X-Admin-Token)* — list all accounts (active + soft-deleted).
- `DELETE /admin/broker-accounts/{broker_account_id}` *(X-Admin-Token)* — soft-delete (`is_active=False`); history stays queryable, cron loops skip it.

**Account scoping:** read endpoints (`/positions`, `/gap`, `/drift`, `/suggestions`, `/indicators`) take an optional `?broker_account_id` (default: primary). Job triggers (`run-daily-report`, `run-weekly-suggestions`, `run-auto-trade`) and bulk mutations (`cancel-all-orders`, `reset-week-suggestions`, `emergency-stop`) default to **all active brokers**, or one via `?broker_account_id`. Invalid/inactive id → 404.

### `GET /positions`

Latest snapshot per ticker for one broker account (`?broker_account_id`, default primary), ordered by portfolio weight descending.

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

### `POST /admin/run-movers` *(requires X-Admin-Token)*

Manually triggers the movers email job (detect threshold crossings → fetch news → triage → email).

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" -X POST localhost:8000/admin/run-movers
```

### `POST /admin/run-weekly-review` *(requires X-Admin-Token)*

Manually triggers the Friday weekly review email. Per broker account: omit `?broker_account_id`
to send one review per active broker (subjects prefixed `[nickname]`), or pass
`?broker_account_id=<id>` to send just that account's. The user-level market context is built
once and shared across the per-account emails.

### `POST /admin/reconcile/{execution_id}` *(requires X-Admin-Token)*

Manually link an `order_execution` row to a specific `order_suggestion`. Use when the automatic reconciliation flags a fill as `manual_review` (ambiguous heuristic match).

```bash
curl -X POST localhost:8000/admin/reconcile/42 \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"suggestion_id": 7}'
```

### `POST /admin/run-auto-trade` *(requires X-Admin-Token)*

Manually trigger one auto-trade pass (respects the current mode — no-ops if `OFF`).

### `POST /admin/auto-trade/promote` *(requires X-Promotion-Token)*

Promote (or demote) the auto-trade mode. `broker_scope` accepts only `alpaca_paper` in
this build (`Literal["alpaca_paper"]` on `AutoTradePromoteRequest`; any other value is
rejected with 422 before the soak-window check ever runs) — the private build's
`alpaca_live` and `moomoo` scopes are not reachable here:

| `broker_scope` | `to_mode` | Min days in current mode |
|---|---|---|
| `alpaca_paper` | `DRY_RUN` | 0 (first promotion) |
| `alpaca_paper` | `LIVE` | 0 |

Both windows are `0` because `alpaca_paper` has no real money — this build cannot
promote beyond paper regardless of soak time (`SOAK_WINDOWS` in `main.py`; see
[ADR-0036](docs/adr/0036-paper-only-public-build.md)). The promotion machinery itself —
mode transitions, the promotion-log audit trail, the demotion path — is unchanged from
ADR-0014 and still runs; only the private build's non-zero windows (`alpaca_paper` →
`LIVE`: 14 days, `alpaca_live` → `LIVE`: 28 days) are absent here. Demotion to `OFF` is
always immediate. The endpoint still returns 409 with `days_remaining` when a
configured window is unmet, but with both windows at `0` that path is unreachable in
this build.

```bash
curl -X POST localhost:8000/admin/auto-trade/promote \
  -H "X-Promotion-Token: $AUTO_TRADE_PROMOTION_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"to_mode": "DRY_RUN", "broker_scope": "alpaca_paper", "reason": "starting soak"}'
```

### `POST /admin/auto-trade/caps` *(requires X-Promotion-Token)*

Update spending caps (closes the old row, inserts a new one):

```bash
curl -X POST localhost:8000/admin/auto-trade/caps \
  -H "X-Promotion-Token: $AUTO_TRADE_PROMOTION_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"per_order_max_usd": 500, "per_day_max_usd": 1500, "per_week_max_usd_per_ticker": 1000, "per_day_max_orders": 5}'
```

### `POST /admin/cancel-all-orders` *(requires X-Admin-Token)*

Cancels every open broker order (`accepted_for_routing`, `dry_run=False`). Sets the matching `order_execution` row status to `broker_cancelled` so the idempotency guard is cleared — auto-trade will re-place the order on its next run. Does **not** change auto-trade mode.

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/admin/cancel-all-orders
# → {"cancelled": ["ord-abc"], "failed": [], "total_cancelled": 1, "total_failed": 0}
```

### `POST /admin/reset-week-suggestions` *(requires X-Admin-Token)*

Cancels open broker orders for the current week and resets those suggestions to `pending`. Use when limit prices are stale mid-week and you want to reconsider suggestions before the next Sunday run. (`/admin/reset-week-buy-suggestions` remains as a backward-compat alias.)

Query param: `side` — `"buy"` (default), `"sell"`, or `"all"`. Default `"buy"` preserves the original behaviour.

- Finds all `accepted` suggestions matching the side filter for `week_of = _next_monday()`.
- For each, cancels the linked `accepted_for_routing` broker order (`dry_run=False`). Cancel failure is logged but does not block the reset.
- Sets `OrderSuggestion.status → "pending"`, clears `acted_at`.

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/admin/reset-week-suggestions
# → {"week_of": "2026-05-25", "suggestions_reset": [12, 13], "orders_cancelled": ["ord-abc"], "cancel_failed": []}

# Reset sell suggestions too
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" "localhost:8000/admin/reset-week-suggestions?side=sell"

# Reset all sides
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" "localhost:8000/admin/reset-week-suggestions?side=all"
```

### `POST /admin/resend-weekly-email` *(requires X-Admin-Token)*

Re-renders and re-sends the weekly suggestions email from existing DB rows — no LLM, no bar updates, no suggestion regeneration. Useful for testing template layout changes or resending after a template fix.

- Reads `pending`/`accepted` suggestions for the current `week_of` from DB (returns 404 if none exist).
- Recomputes indicators and nearby S/R levels (fast — reads Parquet bars only).
- Uses the persisted `llm_rationale` if available, otherwise falls back to the technical `reason` string.
- Subject is prefixed `[Resend]` to distinguish from the real Sunday run.

```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/admin/resend-weekly-email
# → {"status": "ok", "week_of": "2026-05-25", "suggestions_sent": 4, "message": "Resent 4 suggestions for 2026-05-25"}
```

### `POST /admin/auto-trade/emergency-stop` *(requires X-Admin-Token)*

Immediately fires the kill switch: flips mode to `OFF`, cancels all open auto-trade orders placed in the last 24h, writes a `kill_switch_log` row, and sends an alert email. Recovery requires manual re-promotion.

Interactive docs: `http://localhost:8000/docs`

---

## Daily email report

Fires Mon–Fri at 16:15 America/New_York. Contains:

| Section | Content |
|---|---|
| Header | Date, equity, cash, broker/mode |
| Drift alerts | Yellow banner — tickers outside their rebalance band |
| Untracked positions | Red banner — positions held with no target allocation; prompts to add to `targets.yaml` or trim |
| Open & committed orders | This-week accepted suggestions + their broker order state, with un-accept links |
| Allocation | **Donut chart of current allocation incl. cash** (inline image — ADR-0025) + legend, then the allocation table (ticker, qty, market value, current %, target %, gap %, band status) |
| Gap summary | Top 3 underweight + top 3 overweight |
| Orders this week | Counts (placed / filled / $ filled notional) + a fills table — replaces the old S/R "Levels at a glance" (levels are weekly-only now) |
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
| Top-Up Opportunities | Sentiment-sized near-target buys (kind=topup) with band-headroom qty; ★ STRONG ENTRY highlight when anchor conf ≥ 0.75 and no bearish news |
| Candle-aware levels | "Current" shows the day's range (close + low–high); Nearest Support/Resistance show touch history (Nx/30d, touched today); supports recently closed-through are excluded as buy anchors |
| Earnings warning | Amber box lists watchlist tickers reporting this week or next (Finnhub); ★ marks those that also have a suggestion in the email |
| Reflection / lessons *(opt-in, off by default)* | Weekly-review section reviews resolved suggestions vs fills/news/current price; Sonnet extracts methodology lessons (no trade advice) into a `reflection_insight` table. The lessons are fed back only into the next reflection — they never reach the suggestion engine. Enable with `REFLECTION_ENABLED=true` |
| Levels at a glance | SMA-50/200 distance, nearest support and resistance per watchlist ticker |
| Footer | Reminder that execution is manual |

Subject: `Orders for the week of MMM DD`

Each suggestion row has **Accept** (green) and **Reject** (grey) buttons. Clicking one updates the suggestion status directly via an HMAC-signed magic link — no login required. Links expire after 7 days; second click returns a "already acted" message.

Suggestions use "half-the-gap" sizing: each order deploys half the dollar shortfall (or surplus). The limit price is chosen by `select_anchor()`: Claude Sonnet 4.6 scores all computed S/R levels for confidence, and the highest-confidence level within 8% of the current price is used (buy orders use support levels; sell orders use resistance levels). Scoring is news-augmented — bearish news reduces support confidence; bullish news reduces resistance confidence. If LLM scoring fails, the system falls back to nearest-distance selection.

Before suggestions are persisted and emailed they pass through the **suggestion review graph** (`graphs/suggestion_review.py`):

```
gather_context → reason (Sonnet) → context_adjust → critic (Sonnet) → revise or skip_revise → finalize
```

- **gather_context**: Materialises gap rows, scored levels, material news, indicators, account, and untracked positions into a frozen `ReviewContext` before any LLM node runs. Also fetches the upcoming-earnings calendar from Finnhub and loads the most recent persisted Friday market context.
- **reason**: Writes a 2–4 sentence rationale per draft citing specific evidence (confidence score, RSI, news sentiment, MA distance, gap %). Rationales appear as the "Rationale" column in the weekly email.
- **context_adjust** *(Phase 4.7)*: Three sub-passes — (a) **earnings gate**: if a ticker has earnings within `EARNINGS_LOOKAHEAD_DAYS`, Python halves the qty (`EARNINGS_SIZE_FACTOR`) and optionally moves the anchor to a deeper S/R level; (b) **narrative multiplier**: if a fresh Friday context exists, Sonnet returns a `size_multiplier` that Python clamps to `[CONTEXT_SIZE_MIN, CONTEXT_SIZE_MAX]`; (c) **apply**: factors are combined, qty is rounded down, sub-1-share drafts are dropped, and rationale indices are re-keyed. Adjusted suggestions carry `base_qty`, `size_factor`, and `context_note` in both the DB and the email.
- **critic**: Reviews all drafts as a set; emits `approve / revise / reject` with structured `suggested_changes` (e.g. `{"anchor_method": "sma_50"}`). Calibration target: 10–25% revise-or-reject rate per weekly run. Rule 6 (v2 prompt): respect prior defensive shrinks from context_adjust.
- **revise**: Deterministic Python applies the critic's changes, validating every field against known scored levels. Invented prices or unknown methods are silently rejected (original draft kept). **LLMs propose changes; Python applies them.**
- **finalize**: Persists approved and revised drafts via `persist_suggestions()`.

The mechanical `order_suggestion.reason` stays in the DB as the immutable audit trail; the Sonnet-written rationale appears in the email. If the reason node fails, the email falls back to the mechanical reason. If no fresh Friday context exists (first run, stale, or `TAVILY_API_KEY` not set), the narrative sub-pass is silently skipped; the earnings gate still applies when `FINNHUB_API_KEY` is set.

**Email display**: When a suggestion was size-adjusted, the qty cell shows `N (base B · ×F)` and a grey `context_note` line appears below the rationale. In plain-text email the format is `N (base B xF.FF)` inline.

See [ADR-0006](docs/adr/0006-sr-methodology.md), [ADR-0007](docs/adr/0007-position-sizing.md), [ADR-0013](docs/adr/0013-suggestion-review-pipeline.md), and [ADR-0021](docs/adr/0021-context-aware-order-sizing.md) for the full methodology.

---

## Movers email

Fires Mon–Fri at 16:30 America/New_York (15 min after bars are updated). Sends only when a watchlist ticker crosses a **new** threshold milestone — not every day the same move persists.

On **Monday** runs the news lookback window is extended to **48 hours** (vs. 24 h on other days) to catch Friday-afternoon and weekend news that often drives Monday opening moves.

### Tiered threshold logic

| Scenario | Outcome |
|---|---|
| Ticker at +6%, no prior state | Crosses 5% milestone → email sent, threshold stored as 5.0 |
| Ticker still at +6% next day | Next milestone is 10% — not crossed → no email |
| Ticker climbs to +11% | Crosses 10% milestone → email sent, threshold stored as 10.0 |
| Ticker drops back to +2% | abs(pct) < 5% → threshold reset to 0.0 |
| Ticker moves to +6% again | Crosses 5% milestone again → email sent |

The `mover_state` table tracks `last_triggered_threshold` per ticker. No email is sent if no ticker crossed a new milestone that day.

### News triage graph (LangGraph)

For each mover, recent news (last 24h) is fetched from Alpaca News (primary) or Finnhub (fallback) and run through a three-node LangGraph:

```
classify (Haiku) → critic (Haiku) → [conditional] arbitrate (Sonnet)
```

- **classify**: Batch-classifies up to 20 most-recent headlines as `is_material`, `sentiment`, and `summary`
- **critic**: Reviews classifier output; flags items with suspicious `is_material=true`, unsupported sentiment, or hallucinated entities
- **arbitrate**: Only invoked when the critic flags items; Sonnet re-evaluates flagged items for a final decision

Graph state is held in memory (`MemorySaver`) — ephemeral per invocation, no SQLite write contention. Target critic-flagging rate: 10–30%.

### Email content

One card per mover showing:
- Ticker, % change, today's close vs. last week's close
- Top 3 `llm_material=true` headlines with LLM summary and sentiment badge (bullish/bearish/neutral)
- "No material news in the last 24h" when nothing material is found

---

## Weekly review email (Friday)

Fires Friday at 17:00 America/New_York. A backward-looking reflection on the week — distinct from the Sunday suggestions email (which is forward-looking).

| Section | Content |
|---|---|
| 1. Header | Week-of date, account equity, total realised PnL (green / red) |
| 2. Suggestions vs fills | Ticker, suggested qty/price, user action, fill outcome |
| 3. Drift state | Current vs target allocation, band status for each ticker |
| 4. Material news | LLM-material events for held tickers this week |
| 5. Next Sunday preview | Suggestions run without persisting (non-authoritative, labelled as such) |
| 6. Auto-trade activity | Mode changes, placements, cap spend, kill-switch events if any |
| 7. **Order Activity** *(Phase 4.8)* | Suggestion funnel (suggested→accepted→routed→filled, DRY_RUN labelled separately), dollar flow (buy/sell routed vs filled), allocation drift table (Mon→Fri per ticker, "→ closer/farther"), per-ticker breakdown, 4-week trend strip |
| 8. Weekly market context | Macro/Fed narrative, sector summary, per-ticker catch-up, next-week events; sources cited. Omitted if `TAVILY_API_KEY` not set. **Persisted to `weekly_market_context` table** (keyed to the upcoming Monday) so Sunday's suggestion graph can read it. |
| 9. Reflection — lessons *(opt-in)* | How this week's resolved calls played out, plus Sonnet-extracted methodology lessons. Omitted entirely unless `REFLECTION_ENABLED=true`; costs one Sonnet call per account per week and does not influence any suggestion. |

A further section, Moomoo parallel-run status, existed in the private build's email while
the Moomoo adapter was soaking (ADR-0018); it does not ship in this build (ADR-0036).

Subject: `Weekly Review — week of MMM DD`

---

## Reconciliation and realised PnL

After market close (16:45 ET), the reconciliation engine fetches fills from the broker and matches them to `order_suggestion` rows using four rules (priority order):

1. `client_order_id` matches `sug-N` or `sug-N-rK` (retry) — auto-trade placed (confidence 1.0)
2. Single accepted suggestion with same ticker + side, within 48h of suggestion creation, price within ±0.5% (confidence 0.9)
3. Multiple candidates → best-proximity match flagged `manual_review` (confidence 0.5)
4. No match → `untracked` (confidence 0.0)

For each sell fill, FIFO cost basis is computed against prior buy lots to produce `realized_pnl_usd`. `manual_review` rows surface in the Friday weekly review for user inspection. Use `POST /admin/reconcile/{id}` to resolve ambiguous matches.

---

## Opt-in auto-trade

Auto-trade is **off by default**. Promotion requires a separate `AUTO_TRADE_PROMOTION_TOKEN` and soak-window enforcement (see endpoint docs above).

**Mode lifecycle:** `OFF` → `DRY_RUN` (simulates orders, no broker calls) → `LIVE` (real orders via broker API)

Each auto-trade pass (09:35 ET Mon–Fri) processes `accepted` suggestions through five guards before placing:

1. **Idempotency** — skips if an execution row for `sug-N` already exists
2. **Wash-sale** — skips buy if a real loss-sell on the same ticker occurred within 30 calendar days
3. **Caps** — per-order, per-day, per-week-per-ticker, per-day order count
4. **Cash sufficiency** — skips if buying power < order cost

Guard failures skip the individual suggestion without affecting mode. Four events trigger the **kill switch** (mode → OFF + cancel open orders + alert email): `broker_error`, `readback_mismatch`, `readback_failed`, `manual`.

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

All transactional tables are in the SQLite database (`investor.db`). In Docker it lives on the `dbdata` named volume at `/app/db/investor.db`; locally it defaults to `data/investor.db`. It runs in **DELETE journal mode with `synchronous=FULL`, never WAL** (ADR-0026).

### `target_allocation` / `broker_account` / `positions_snapshot` / `meta`

See Phase 1 for full column docs. Phase 2: unchanged.

**Phase 4.9a Stage A (multi-broker foundation):**

- A `broker_account_id` partition column is added to `target_allocation`, `positions_snapshot`, `order_suggestion`, and `order_execution`. It is a plain integer column (no DB FK — referential integrity is app-enforced, per the codebase convention) that points at `broker_account.account_ref`. Existing rows are backfilled to the single Alpaca account by migration `d8589fe198cf`.
- `broker_account` becomes **dual-purpose**: identity + time-versioned state in one table. A stable `account_ref` (constant across an account's close-and-insert state rows) is the partition key; `id` still changes on each state insert and must never be an FK target. New identity columns: `nickname`, `is_active` (soft-delete flag), `connection_config` (JSON naming the broker's env-var credentials). The latest open row (`effective_to IS NULL`) is the source of truth for both identity and current cash/equity.
- `order_suggestion`'s unique constraint becomes `(broker_account_id, week_of, ticker, side)` so two brokers can each suggest the same ticker in the same week.

> Every reader, job, and writer is scoped by `broker_account_id` (Stages B/B-API/B8). The column is `NOT NULL` (migration `6a4a9fada1dc`) now that all writers set it.

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
| `anchor_method` | varchar | Scored level method used as limit-price anchor, e.g. `sma_50` (Phase 3c; NULL on older rows) |
| `base_qty` | double | Pre-adjustment qty before context_adjust node ran; NULL when no size adjustment was made (Phase 4.7) |
| `size_factor` | double | Combined size multiplier applied by context_adjust (1.0 = no adjustment; Phase 4.7) |
| `context_note` | text | Human-readable audit note explaining the size adjustment, e.g. "earnings 2026-06-01, ×0.50" (Phase 4.7) |

### `llm_call_log` (Phase 3a)

One row per LLM API call. Used for cost tracking and debugging.

| Column | Type | Description |
|---|---|---|
| `ts` | timestamptz | Call timestamp (UTC) |
| `purpose` | varchar | `score_levels`, `news_classify`, `news_critic`, `news_arbitrate`, `suggestion_reason`, `suggestion_critic`, `context_adjust`, or `weekly_context` |
| `model` | varchar | e.g. `claude-haiku-4-5`, `claude-sonnet-4-6` |
| `prompt_hash` | varchar | First 12 hex chars of SHA-256 of system+user prompt |
| `input_tokens` | int | Prompt tokens consumed |
| `output_tokens` | int | Completion tokens produced |
| `cost_usd` | float | Estimated USD cost |
| `latency_ms` | int | Wall-clock latency |
| `status` | varchar | `ok` / `schema_error` / `api_error` |
| `error` | text | Error message if status ≠ `ok` |

### `news_event` (Phase 3b)

One row per fetched news article. `url_hash` is unique — re-running the job is idempotent.

| Column | Type | Description |
|---|---|---|
| `id` | int | Primary key |
| `ts` | timestamptz | Row insertion timestamp (UTC) |
| `ticker` | varchar | e.g. `AAPL` |
| `published_at` | timestamptz | Article publication time |
| `source` | varchar | `alpaca` or `finnhub` |
| `headline` | varchar | Article headline |
| `url` | varchar | Article URL |
| `url_hash` | varchar | SHA-256(normalised_url)[:16] — unique key for dedup |
| `llm_material` | bool | Whether the article is material (from classify or arbitrate node) |
| `llm_sentiment` | varchar | `bullish`, `bearish`, `neutral`, or null |
| `llm_summary` | varchar | One-sentence factual summary (≤25 words) |
| `llm_model` | varchar | Model that produced the classification |
| `llm_cost_usd` | float | Estimated LLM cost for this article's share of the batch |
| `arbitrated` | bool | `true` if Sonnet arbitrate node re-evaluated this article |

### `mover_state` (Phase 3b)

One row per watchlist ticker. Tracks the tiered threshold state for movers detection.

| Column | Type | Description |
|---|---|---|
| `ticker` | varchar | Primary key |
| `last_triggered_threshold` | float | Most recent milestone crossed (5.0, 10.0, 15.0, …); 0.0 = never or reset |
| `last_triggered_at` | timestamptz | When the threshold was last crossed |
| `last_pct_change` | float | % change at the time of the last trigger |

### `weekly_market_context` (Phase 4.7)

Append-only. One row written each Friday by `run_weekly_review` when a Tavily context is successfully built. Keyed to the *upcoming* Monday so Sunday's graph can load it by matching `week_of`. Multiple rows per week are allowed (re-runs); the loader picks the most recent one within `CONTEXT_MAX_AGE_DAYS`.

| Column | Type | Description |
|---|---|---|
| `id` | int | Primary key |
| `week_of` | date | The Monday of the upcoming trading week (Friday→Sunday bridge key) |
| `payload_json` | text | Full `WeeklyMarketContext` serialised as JSON (macro/sector/per-ticker/forward events/citations) |
| `created_at` | timestamptz | Row insertion timestamp (UTC) |

### `order_execution` (Phase 4)

One row per broker fill. Written by the reconciliation engine (for manual trades) and by the auto-trade engine (for automated placements). The `(broker_order_id, broker)` pair is a unique idempotency key.

| Column | Type | Description |
|---|---|---|
| `id` | int | Primary key |
| `suggestion_id` | int | FK → `order_suggestion.id` (nullable — untracked fills have no suggestion) |
| `ticker` | varchar | e.g. `AAPL` |
| `side` | varchar | `buy` or `sell` |
| `filled_qty` | double | Shares actually filled |
| `filled_price` | double | Average fill price |
| `filled_at` | timestamptz | Fill timestamp (UTC) |
| `broker` | varchar | `alpaca`, `moomoo`, or `dry_run` (`moomoo` does not ship in this build — see [ADR-0036](docs/adr/0036-paper-only-public-build.md)) |
| `broker_order_id` | varchar | Broker-assigned order ID (NULL for dry-run rows) |
| `client_order_id` | varchar | `sug-N` for auto-trade rows; NULL or custom for manual fills |
| `dry_run` | bool | `true` for simulated DRY_RUN orders; `false` for real fills |
| `status` | varchar | `filled`, `partially_filled`, `rejected`, `expired`, `accepted_for_routing`, or `dry_run` |
| `realized_pnl_usd` | double | FIFO realised PnL for sell fills (NULL for buys or when cost basis is unavailable) |
| `match_method` | varchar | `auto_matched`, `manual_review`, `untracked`, `auto_trade_placed`, or `manual_matched` |
| `match_confidence` | float | Confidence score from reconciliation rules [0.0, 1.0] |
| `created_at` | timestamptz | Row insertion timestamp |

### `auto_trade_promotion_log` (Phase 4)

Append-only audit log of every auto-trade mode change.

| Column | Type | Description |
|---|---|---|
| `id` | int | Primary key |
| `ts` | timestamptz | When the promotion occurred |
| `from_mode` | varchar | Previous mode (`OFF`, `DRY_RUN`, `LIVE`) |
| `to_mode` | varchar | New mode |
| `broker_scope` | varchar | `alpaca_paper`, `alpaca_live`, or `moomoo` in the private build; this build's `POST /admin/auto-trade/promote` accepts only `alpaca_paper` (`Literal["alpaca_paper"]` on `AutoTradePromoteRequest` in `main.py`, backed by `config.VALID_BROKERS` at startup) — see [ADR-0036](docs/adr/0036-paper-only-public-build.md) |
| `reason` | varchar | Human-supplied reason |
| `actor` | varchar | Always `admin` in Phase 4 (single-user) |

### `kill_switch_log` (Phase 4)

Permanent audit of every kill-switch activation.

| Column | Type | Description |
|---|---|---|
| `id` | int | Primary key |
| `ts` | timestamptz | Activation timestamp |
| `trigger` | varchar | `broker_error`, `readback_mismatch`, `readback_failed`, or `manual` |
| `detail` | text | Human-readable context |
| `cancelled_order_ids` | text | JSON array of broker order IDs cancelled at trigger time |

### `auto_trade_caps` (Phase 4)

Time-versioned spending caps. Only one row has `effective_to = NULL` (the active cap). Updating caps closes the old row and inserts a new one.

| Column | Type | Description |
|---|---|---|
| `id` | int | Primary key |
| `per_order_max_usd` | double | Max USD per single order |
| `per_day_max_usd` | double | Max total USD across all orders in one day |
| `per_week_max_usd_per_ticker` | double | Max USD for one ticker in one calendar week |
| `per_day_max_orders` | int | Max number of orders in one day |
| `effective_from` | timestamptz | When this cap row became active |
| `effective_to` | timestamptz | When it was superseded (NULL = currently active) |

### `auto_trade_state` (Phase 4.9a)

Per-broker auto-trade mode + optional cap overrides. One row per broker account (keyed by `broker_account.account_ref`). Replaces the single `meta.auto_trade_mode` key, so each broker runs its own OFF → DRY_RUN → LIVE soak ladder independently — promoting Alpaca does not promote Moomoo (Moomoo does not ship in this build; see [ADR-0036](docs/adr/0036-paper-only-public-build.md)). Cap-override columns are nullable; when NULL the engine falls back to the global `auto_trade_caps` row. The mode read/write path (`_get_mode` / `set_mode` / promote / kill switch) is wired to this table.

| Column | Type | Description |
|---|---|---|
| `broker_account_id` | int | PK = `broker_account.account_ref` |
| `mode` | varchar | `OFF` / `DRY_RUN` / `LIVE` (seeded from the old `meta.auto_trade_mode`) |
| `promoted_at` | timestamptz | Last promotion timestamp |
| `promotion_soak_complete_at` | timestamptz | When the current mode's soak window completes |
| `last_kill_switch_event` | timestamptz | Last kill-switch activation for this broker |
| `per_order_cap_usd` / `per_day_cap_usd` / `per_week_per_ticker_cap_usd` / `per_day_order_count_cap` | double / int | Per-broker overrides; NULL = use global `auto_trade_caps` |

---

## Inspecting the database

Local run: `sqlite3 data/investor.db`. Docker (db is on the `dbdata` volume — query inside the container):

```bash
# one-off query
docker compose exec app uv run python -c "import sqlite3; \
  print(sqlite3.connect('/app/db/investor.db').execute('SELECT COUNT(*) FROM order_suggestion').fetchone())"

# or an interactive shell against a copy
docker run --rm -it -v me_invest_dbdata:/db keinos/sqlite3 sqlite3 /db/investor.db
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

-- recent movers with news triage results
SELECT ne.ticker, ne.published_at, ne.headline, ne.llm_material,
       ne.llm_sentiment, ne.llm_summary, ne.arbitrated
FROM news_event ne
WHERE ne.ts >= datetime('now', '-1 day')
ORDER BY ne.ticker, ne.published_at DESC;

-- mover threshold state per ticker
SELECT ticker, last_triggered_threshold, last_pct_change, last_triggered_at
FROM mover_state ORDER BY ticker;

-- LLM cost by purpose (includes suggestion_reason + suggestion_critic from Phase 3c)
SELECT purpose, model, date(ts) AS day,
       sum(cost_usd) AS total_usd, count(*) AS calls
FROM llm_call_log GROUP BY 1, 2, 3 ORDER BY 3 DESC, 4 DESC;

-- suggestion anchor methods (Phase 3c) — which levels were chosen
SELECT ticker, side, anchor_method, round(confidence_at_creation, 2) AS conf,
       limit_price, status
FROM order_suggestion
WHERE week_of = date('now', 'weekday 1', '-7 days')
ORDER BY ticker;

-- recent fills from the reconciliation engine (Phase 4)
SELECT oe.ticker, oe.side, oe.filled_qty, oe.filled_price, oe.filled_at,
       oe.match_method, round(oe.match_confidence, 2) AS conf,
       oe.realized_pnl_usd, oe.dry_run
FROM order_execution oe
WHERE oe.dry_run = false
ORDER BY oe.filled_at DESC LIMIT 20;

-- check current auto-trade mode
-- Phase 4 (pre-4.9a): SELECT value FROM meta WHERE key = 'auto_trade_mode';
-- Phase 4.9a Stage A onward (per-broker):
SELECT ats.broker_account_id, ba.nickname, ats.mode
FROM auto_trade_state ats
JOIN broker_account ba ON ba.account_ref = ats.broker_account_id
WHERE ba.effective_to IS NULL;

-- realised PnL summary by ticker (Phase 4)
SELECT ticker, sum(realized_pnl_usd) AS total_pnl, count(*) AS sell_fills
FROM order_execution
WHERE side = 'sell' AND dry_run = false AND realized_pnl_usd IS NOT NULL
GROUP BY ticker ORDER BY total_pnl DESC;

-- fills awaiting manual review (Phase 4)
SELECT id, ticker, side, filled_qty, filled_price, filled_at, match_confidence
FROM order_execution
WHERE match_method = 'manual_review' AND dry_run = false
ORDER BY filled_at DESC;

-- persisted weekly market contexts (Phase 4.7)
SELECT id, week_of, created_at, length(payload_json) AS payload_bytes
FROM weekly_market_context
ORDER BY created_at DESC LIMIT 10;

-- suggestions with context-driven size adjustments this week (Phase 4.7)
SELECT ticker, side, qty, base_qty, size_factor, limit_price, anchor_method, context_note, status
FROM order_suggestion
WHERE week_of = date('now', 'weekday 1', '-7 days')
  AND size_factor != 1.0
ORDER BY ticker;

-- LLM cost for context_adjust calls (Phase 4.7)
SELECT date(ts) AS day, sum(cost_usd) AS total_usd, count(*) AS calls
FROM llm_call_log
WHERE purpose = 'context_adjust'
GROUP BY 1 ORDER BY 1 DESC;

-- suggestion funnel for the current operations week (Phase 4.8)
SELECT
  count(*) AS suggested,
  count(*) FILTER (WHERE status = 'accepted')  AS accepted,
  count(*) FILTER (WHERE status = 'rejected')  AS rejected,
  count(*) FILTER (WHERE status = 'expired')   AS expired
FROM order_suggestion
WHERE week_of = date('now', 'weekday 1', '-7 days');

-- allocation drift: target vs Friday weight per ticker (Phase 4.8)
WITH latest AS (
  SELECT ticker, market_value,
         ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY ts DESC) AS rn
  FROM positions_snapshot
)
SELECT t.ticker, t.target_pct,
       round(l.market_value / sum(l.market_value) OVER () * 100, 2) AS current_pct,
       round(t.target_pct - l.market_value / sum(l.market_value) OVER () * 100, 2) AS gap_pp
FROM target_allocation t
LEFT JOIN latest l ON l.ticker = t.ticker AND l.rn = 1
WHERE t.effective_to IS NULL
ORDER BY t.target_pct DESC;

-- open executions still at broker (accepted_for_routing) — should be empty after expiry sweep (Phase 4.8)
SELECT oe.id, oe.ticker, oe.side, oe.broker_order_id, oe.created_at
FROM order_execution oe
WHERE oe.status = 'accepted_for_routing' AND oe.dry_run = false
ORDER BY oe.created_at;
```

---

## Project layout

```
src/investor/
  main.py             FastAPI app + lifespan
  config.py           pydantic-settings + targets.yaml loader
  db.py               SQLite engine + session factory
  models.py           SQLAlchemy ORM models (Phase 3b: NewsEvent, MoverState; Phase 3c: anchor_method; Phase 4: OrderExecution, AutoTradePromotionLog, KillSwitchLog, AutoTradeCaps; Phase 4.7: WeeklyMarketContextRow, OrderSuggestion.base_qty/size_factor/context_note; Phase 4.9a: AutoTradeState, broker_account_id partition key on 4 tables, broker_account.account_ref/nickname/is_active/connection_config)
  scheduler.py        APScheduler bootstrap
  safety.py           paper-only invariant (L0–L3) — see ADR-0036
  brokers/
    base.py           BrokerAdapter Protocol + dataclasses (Activity, OrderRequest, OrderConfirmation)
    alpaca.py         AlpacaAdapter
  graphs/
    __init__.py           make_checkpointer() — MemorySaver (in-memory, avoids SQLite write contention)
    _nodes.py             llm_node_call() — generic LLM node helper (Phase 3a lessons applied)
    news_triage.py        Three-node triage graph: classify → critic → conditional arbitrate
    suggestion_review.py  Six-node review graph: gather_context → reason → context_adjust → critic → revise/skip → finalize (Phase 4.7: context_adjust node, earnings gate, narrative multiplier)
  prompts/
    score_levels_v1.txt       Sonnet 4.6 scoring prompt (hard rules: no invented prices, no trade recs)
    score_levels_v2.txt       News-augmented scoring prompt (bearish news ↓ support conf; bullish ↓ resistance conf)
    news_classify_v1.txt      Haiku batch-classifier prompt
    news_critic_v1.txt        Haiku critic prompt (flag 10–30% of items)
    news_arbitrate_v1.txt     Sonnet final-decision prompt for flagged items
    suggestion_reason_v1.txt  Sonnet per-draft rationale prompt (2–4 sentences, cite evidence)
    suggestion_critic_v1.txt  Sonnet cross-draft critic prompt (five severity-ordered criteria)
    suggestion_critic_v2.txt  v1 + rule 6: respect prior context_adjust defensive shrinks (Phase 4.7)
    context_size_v1.txt       Sonnet size-multiplier prompt — bounded [min, max], prefer_anchor from scored_levels only (Phase 4.7)
    weekly_context_v1.txt     Sonnet weekly market context synthesis prompt (no price targets; JSON output) (Phase 4.5)
  services/
    snapshot.py       Position + account ingestion
    gap.py            Gap computation + UntrackedPosition detection
    analytics.py      DuckDB context manager (price_bar view over Parquet)
    indicators.py     IndicatorRow + compute_indicators() — SMA/EMA/RSI/MACD
    levels.py         SRLevelRow, NearbyLevels + compute_levels() / persist_levels()
    llm.py            LLMClient Protocol + AnthropicAPIClient + AgentSDKClient + make_llm_client() factory + persist_llm_call_log()
    llm_levels.py     ScoredLevel + score_levels_for_ticker() (news-augmented, v2 prompt); load_latest_scored_levels()
    magic_link.py     sign_action() / verify_action() — HMAC-SHA256 email tokens
    news.py           NewsRaw, fetch_alpaca_news(), fetch_finnhub_news(), get_news_for_movers(), load_recent_material_news()
    suggest.py        OrderSuggestionRow (+ base_qty/size_factor/context_note in Phase 4.7), select_anchor() + generate_suggestions() / persist_suggestions()
    daily_report.py   DailyReport + compose_daily_report() — orders-this-week recap + allocation_slices (Phase 4.9a+)
    charts.py         build_allocation_pie() — Pillow donut PNG for the daily email (ADR-0025)
    bars.py           update_bars() — smart backfill + incremental Parquet append
    targets.py        Hash-based idempotent target loader
    render.py         Jinja2 template rendering
    email.py          SMTPEmailer + FakeEmailer — inline CID images via multipart/related (ADR-0025)
    reconciliation.py MatchResult + reconcile_activities() / persist_reconciliation() / compute_realized_pnl() / sync_open_order_statuses() (Phase 4 + 4.8)
    auto_trade.py     AutoTradeOutcome + run_auto_trade_pass() + guards + _trigger_kill_switch() (Phase 4)
    tavily.py         TavilyClient Protocol + TavilyConcreteClient + FakeTavilyClient + factory (Phase 4.5)
    weekly_context.py WeeklyMarketContext + build_weekly_market_context() — Tavily fanout + Sonnet synthesis (Phase 4.5); persist_weekly_context() / load_latest_weekly_context() (Phase 4.7)
    earnings.py       EarningsClient Protocol + FinnhubEarningsClient + FakeEarningsClient + make_earnings_client() factory (Phase 4.7)
    weekly_review_metrics.py  OrderFunnel / OrderFlow / AllocationDriftRow / PerTickerWeekRow / WeekTrendRow + 5 compute functions; all queries live, no ORM rows cross session boundary (Phase 4.8)
    accounts.py       AccountInfo + list_active_accounts / resolve_primary_account_ref / resolve_active_account_refs (Phase 4.9a multi-broker)
    orders.py         Broker-order cancellation helper shared by the expiry sweep and un-accept
    unaccept.py       Un-accept an accepted suggestion — cancel any working broker order, mark it cancelled (ADR-0032)
    funds.py          Funds-flow detection via a cash-flow heuristic (ADR-0035)
    sentiment.py      SentimentClient Protocol — VIX + CNN Fear & Greed from the graphdata payload (ADR-0030)
    reflection.py     Weekly-review reflection — resolved suggestion outcomes + news into generalizable lessons
    ticker_names.py   Curated ticker → trading-name map for email annotation
  jobs/
    daily_report.py        Mon-Fri 16:15 ET — sync, compose (orders recap + allocation donut), email
    suggestion_expiry.py   Mon-Fri 09:00 ET — cancel stale GTC orders + expire suggestions (pre-market, before auto-trade)
    movers.py              Mon-Fri 16:30 ET — tiered threshold detection, news triage, email
    reconciliation.py      Mon-Fri 16:45 ET — match broker fills to suggestions, FIFO PnL, sync broker-cancelled executions (Phase 4)
    weekly_review.py       Fri 17:00 ET — reflection email + Order Activity metrics (Phase 4.8); persists WeeklyMarketContext to DB with week_of=_next_monday() (Phase 4.7)
    weekly_suggestions.py  Sun 18:00 ET — indicators, levels, LLM scoring, suggestion review graph, email
    auto_trade.py          Mon-Fri 09:35 ET — place orders for accepted suggestions (Phase 4)
    funds_detection.py     Mon-Fri 18:00 ET — detect deposits/withdrawals from the daily-sync state
    backup.py              Sun 02:00 ET — SQLite backup (6h misfire grace; off-hours, non-urgent)
    sync.py                Not scheduled — position/account sync helper called by the cron jobs above
config/
  targets.yaml        Target allocation (hand-edited)
templates/
  daily_report.html.j2         Daily HTML email
  daily_report.txt.j2          Daily plain-text email
  weekly_suggestions.html.j2   Weekly suggestions HTML email (Accept/Reject buttons)
  weekly_suggestions.txt.j2    Weekly suggestions plain-text email
  movers.html.j2               Movers HTML email (one card per mover, top-3 material headlines)
  movers.txt.j2                Movers plain-text fallback
  weekly_review.html.j2        Weekly review HTML email (8 sections) (Phase 4)
  weekly_review.txt.j2         Weekly review plain-text fallback (Phase 4)
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
  funnel_counts.sql           Suggestion funnel counts for one operations week (Phase 4.8)
  order_flow.sql              Buy/sell notional routed and filled, LIVE vs DRY_RUN (Phase 4.8)
  alloc_drift.sql             Per-ticker allocation drift Mon→Fri with holiday fallback (Phase 4.8)
  per_ticker_breakdown.sql    Per-ticker qty/$ routed and filled for one week (Phase 4.8)
migrations/           Alembic revisions
data/                 bind-mounted, gitignored (NOT the SQLite db — see below)
  bars/               Parquet bar files
  *.duckdb            DuckDB analytics file
(SQLite investor.db lives on the `dbdata` Docker named volume at /app/db, ADR-0026)
tests/
  test_config.py              Settings + YAML loader (8 tests)
  test_gap.py                 Gap computation + band_status, cash-buffer invariant (11 tests)
  test_load_targets.py        Hash-based target dedup (5 tests)
  test_email.py               FakeEmailer + SMTPEmailer (3 tests)
  test_daily_report.py        DailyReport + session-close regression (3 tests)
  test_indicators.py          compute_indicators() with synthetic Parquet (6 tests)
  test_levels.py              Pivot formulas, swing detection, build_nearby_levels (8 tests)
  test_llm.py                 AnthropicAPIClient + AgentSDKClient + make_llm_client factory (41 tests)
  test_magic_link.py          sign_action / verify_action — format, tamper, expiry (12 tests)
  test_suggest.py             generate_suggestions, select_anchor, persist lifecycle (27 tests)
  test_news.py                URL normalization, news fetch mocks, tiered threshold logic (27 tests)
  test_news_triage.py         Per-node unit tests, graph integration, fence/parse regressions (12 tests)
  test_suggestion_review.py   revise/skip_revise nodes, critic routing, reason/critic with mock LLM, session-leak guard (18 tests)
  test_suggestion_expiry.py             Expiry sweep: stale → expired, future → unchanged, GTC cancel, exec status update (9 tests)
  test_weekly_suggestions.py            Parallel scoring wall-clock + failure fallback (2 tests)
  test_reconciliation.py                Reconciliation rules, FIFO PnL, idempotency, dry-run isolation, sug-N-rN Rule 1, sync_open_order_statuses, partial-fill guard (20 tests) (Phase 4 + 4.8)
  test_auto_trade.py                    OFF/DRY_RUN/LIVE modes, all guards, kill-switch triggers, promotion soak (20 tests) (Phase 4)
  test_weekly_review.py                 WeeklyReview + SuggestionAudit frozen dataclasses, pending-past-expiry display (8 tests) (Phase 4 + 4.8)
  test_no_unauthorized_submit_order.py  Grep CI gate: submit_order single-call-site enforcement (1 test) (Phase 4)
  test_paper_only.py                    L0–L3 of the paper-only invariant: adapter, config, both factories, submit-order chokepoint (13 tests)
  test_no_live_trading.py               Grep CI gate: no `paper=False` / `alpaca_live` / `MoomooAdapter` reappears in src/ (3 tests)
  test_tavily.py                        FakeTavilyClient, TavilyConcreteClient, factory, cap enforcement (11 tests) (Phase 4.5)
  test_weekly_context.py                build_weekly_market_context: happy path, empty→None, LLM failure, dedup, cap (5 tests) (Phase 4.5)
  test_earnings.py                      FakeEarningsClient, FinnhubEarningsClient, factory, SDK exception fallback (5 tests) (Phase 4.7)
  test_context_adjust.py                earnings gate, reanchor, narrative clamp, sub-1-share drop, rationale re-keying, price invariant (12 tests) (Phase 4.7)
  test_load_targets.py                  Hash-based target dedup, mid-week suggestion expiry on target change (7 tests) (Phase 4.8)
  test_weekly_review_metrics.py         Order funnel, drift sign/over-correction/fallback, weekday guard, flow zeros, partial-fill trend (11 tests) (Phase 4.8)
  test_reset_week_suggestions.py        reset-week endpoint: buy/sell/all side param, 422 on invalid side (7 tests) (Phase 4.8)
  test_migration_phase4_9a.py           Multi-broker migration: backfill collapses to one account, auto_trade_state seeded, downgrade round-trip (2 tests) (Phase 4.9a)
  test_fresh_schema.py                  Regression: fresh `alembic upgrade head` builds every model table (Alembic is sole schema source) (1 test) (Phase 4.9a)
  test_integration_alpaca.py            Full chain vs live Alpaca paper (1 test, skips without keys)
docs/adr/
  (0034 is reserved for an unbuilt Wave B feature — household target allocation)
  0001-broker-adapter-abstraction.md    BrokerAdapter is the only door to a broker SDK; market data separable from execution; domain IDs ≠ broker IDs (retroactive)
  0002-schema-migrations.md             Three-Tier Storage Architecture — SQLite OLTP / DuckDB analytics / Parquet bars
  0003-sqlite-oltp-duckdb-analytics.md  Schema Migrations with Alembic + SQLite (batch mode for ALTER/DROP COLUMN)
  0004-bar-storage.md
  0005-email-failure-policy.md
  0006-sr-methodology.md      S/R methodology; Phase 3a scoring pass; Phase 3c news-augmented scoring + anchor audit trail
  0007-position-sizing.md     Position sizing; Phase 3a confidence-weighted anchor; Phase 3c anchor_method field + critic refinement
  0008-rebalance-bands.md               Absolute per-ticker bands in targets.yaml over relative bands; band_high doubles as the order-size ceiling; loader enforces band_low ≤ pct ≤ band_high (retroactive)
  0009-llm-guardrails.md      Hard rules for LLM output in the suggestion pipeline
  0010-magic-link-auth.md     HMAC magic-link auth for Accept/Reject email buttons
  0011-news-source-priority.md  Alpaca-primary / Finnhub-fallback; URL normalization dedup
  0012-langgraph-adoption.md    LangGraph decision rule, MemorySaver checkpointer, version-pinning
  0013-suggestion-review-pipeline.md  Suggestion review graph; why revise is deterministic Python; calibration target 10–25%
  0014-auto-trade-mode-discipline.md  Three-state mode; OFF default invariant; soak-window matrix; idempotency via sug-N; single-user scope
  0015-kill-switch-design.md          Four kill-switch triggers; guard failures do NOT trigger kill switch; recovery is manual re-promotion
  0016-llm-backend-abstraction.md     LLMClient Protocol, AnthropicAPIClient vs AgentSDKClient, consumer OAuth guardrails
  0017-reconciliation-matching.md     Four matching rules (priority order); FIFO cost-basis; 1h overlap window; sug-N namespace
  0018-moomoo-parallel-run.md         Five soak criteria; OpenD-on-host rule; bars-on-Alpaca; remark↔client_order_id; prefix stripping
  0019-weekly-review-composition.md   Seven sections; Friday-reflection vs Sunday-action cadence; Moomoo-section sunset criteria
  0020-tavily-weekly-context.md       Why Tavily; Protocol swap path; Nebius acquisition risk; informational-only hard constraint
  0021-context-aware-order-sizing.md  Bounded Tavily exception for qty scaling; carved LLM output exception; Finnhub vs free-text earnings
  0022-sentiment-client-and-etf-classification.md  SentimentClient Protocol; CNN F&G fragility contract; ETF classification in targets.yaml
  0023-weekly-order-activity-metrics.md  Allocation drift over fill-rate fiction; live queries over materialised cache; honest manual-placement bucket
  0024-multi-broker-single-user-data-model.md  Dual-purpose broker_account + account_ref partition key; per-broker auto_trade_state + soak ladder; per-broker guard scoping; user-level news/levels/context
  0025-inline-email-images.md          Content-ID inline images over SVG/conic-gradient (Gmail); Pillow donut; multipart/related switch
  0026-sqlite-journaling-and-db-volume.md  WAL is unsafe on Docker bind mounts; journal_mode=DELETE + synchronous=FULL; OLTP db on a named volume
  0027-tavily-movers-fallback.md       Tavily as third fallback in the movers news pipeline
  0028-movers-tiers-direction-and-week-reset.md  Direction-aware movers tiers; reset per ISO week
  0029-split-adjusted-bars.md          Bars stored split-adjusted (Adjustment.SPLIT); SR-level re-backfill procedure
  0030-cnn-sentiment-headers.md        CNN sentiment endpoint with browser-shaped headers; Phase 5 pre-launch removal
  0031-shared-email-components.md      Shared email components and the Jinja autoescape trap
  0032-suggestion-cancelled-status.md  Suggestion status `cancelled` is terminal; auto-trade ignores it
  0033-snapshot-one-ts-per-batch.md    Snapshot one-ts-per-batch contract (alloc_drift reads ts = MAX(ts))
  0035-funds-detection.md              Funds-flow detection via a cash-flow heuristic
  0036-paper-only-public-build.md      Four-layer paper-only invariant (L0–L3); Moomoo adapter removed from this build; ADR-0018/ADR-0024 retained as design record; private build not so constrained
```

---

## Development

```bash
uv sync
uv run pytest                        # 632 unit tests + 1 integration (skipped without API keys)
uv run pytest -m "not integration"   # unit tests only
uv run ruff check --fix
uv run mypy src/
```
