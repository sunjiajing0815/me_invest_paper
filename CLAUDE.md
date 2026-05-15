# Investor Assistant — CLAUDE.md

This file orients Claude Code (and any future LLM agents) to this repository. Read it before making changes.

## Mission

A self-hosted, suggest-only assistant for a long-term US-equities investor. The system pulls positions from a broker (Alpaca first, Moomoo later), compares against a YAML-defined target allocation, identifies support/resistance levels, suggests weekly orders, and emails daily/weekly reports. **The system never places orders.** Order execution is always manual, in the broker's own UI.

Owner: Jane (solo developer, primary user). Multi-tenant productization is Phase 5 — until then, treat this as a single-user app.

## Current phase

Phase 2 is code-complete. See `phase_2_guide.md` for the build plan. The repo's git tag reflects the last completed phase.

Active phases: 0 (foundation), 1 (daily email + bar backfill), 2 (indicators, S/R levels, weekly order suggestions).

## Tech stack and why

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | Best fintech/ML ecosystem |
| API | FastAPI | Lifespan hooks for scheduler bootstrap; OpenAPI for free |
| Scheduler | APScheduler (in-process) | No Redis/Celery needed at single-user scale |
| OLTP DB | SQLite via SQLAlchemy + Alembic | Transactional tables; native Alembic support; built into Python |
| Analytics DB | DuckDB (direct, not via SQLAlchemy) | Vectorized queries over Parquet bars; Phase 1+ |
| Bars (cold storage) | Parquet under `data/bars/` | Survives DB migrations; queryable directly by DuckDB |
| Broker (v1) | Alpaca via `alpaca-py` | REST, no gateway, free paper, fractional shares, AU residents allowed |
| Broker (later) | Moomoo via OpenD on host | Already-funded account for actual long-term capital |
| LLM | Anthropic Claude (Haiku 4.5 triage, Sonnet 4.6 review) | Used in Phase 3+ for news triage. Access via `LLMClient` Protocol (`services/llm.py`): two backends — `AnthropicAPIClient` (default) and `AgentSDKClient` (`claude-agent-sdk`). See ADR-0016. |
| Email | SMTP via Gmail App Password | Phase 1+; plain `smtplib` + Jinja2 templates |
| Container | Single Dockerfile, one `docker-compose.yml` | Same image runs on Mac (Docker Desktop) or VPS |
| Package manager | `uv` | Fast, single source of truth via `pyproject.toml` |

## Repo layout

```
src/investor/
  main.py             FastAPI app + lifespan
  config.py           pydantic-settings + targets.yaml loader
  db.py               SQLite engine + session factory
  models.py           SQLAlchemy ORM models
  scheduler.py        APScheduler bootstrap
  brokers/
    base.py           BrokerAdapter Protocol + dataclasses
    alpaca.py         AlpacaAdapter
    moomoo.py         (future) MoomooAdapter — talks to OpenD on host
  services/
    snapshot.py       position + account ingestion
    gap.py            target-vs-actual gap computation
    analytics.py      DuckDB context manager (price_bar view over Parquet)
    indicators.py     IndicatorRow + compute_indicators() — SMA/EMA/RSI/MACD
    levels.py         SRLevelRow + compute_levels() / persist_levels() / build_nearby_levels()
    suggest.py        OrderSuggestionRow + generate_suggestions() / persist_suggestions()
    daily_report.py   DailyReport dataclass + compose_daily_report()
    bars.py           update_bars() — Alpaca IEX → Parquet append
    targets.py        load_targets_into_db()
    render.py         Jinja2 template rendering
    email.py          SMTPEmailer + FakeEmailer
  jobs/
    daily_report.py   Mon-Fri 16:15 ET — sync, indicators, compose, email
    weekly_suggestions.py  Sun 18:00 ET — indicators, levels, suggestions, email
  graphs/           LangGraph graph definitions and node helpers
config/targets.yaml   user's target allocation (hand-edited or via /targets API)
migrations/           Alembic revisions
scripts/              standalone CLIs (sync_positions, show_gap, load_targets)
docs/adr/             Architecture Decision Records, numbered 0001+
data/                 SQLite file + Parquet bars; bind-mounted; gitignored
tests/                pytest
```

## Common commands

```bash
# bootstrap
uv sync
uv run pre-commit install

# DB
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description"

# one-off scripts
uv run python scripts/load_targets.py
uv run python scripts/sync_positions.py
uv run python scripts/show_gap.py

# server
uv run uvicorn src.investor.main:app --reload --port 8000

# Docker
docker compose build
docker compose up -d
docker compose logs -f app
docker compose down

# tests + lint
uv run pytest
uv run ruff check --fix
uv run mypy src/
```

## Architecture conventions (do not violate without an ADR)

1. **`BrokerAdapter` is the only door to broker SDKs.** No file outside `src/investor/brokers/*` may import `alpaca`, `moomoo`, or any future broker SDK. The rest of the app uses the dataclasses in `brokers/base.py`. This is what makes the eventual Alpaca → Moomoo switchover cheap.

2. **Market data is separable from execution data.** `get_bars` may live on a different adapter than `get_positions`. Even when trading via Moomoo, we may keep using Alpaca for free bars.

3. **Domain IDs ≠ broker IDs.** Tables key on `(ticker)` or `(user_id, ticker)`, never on the broker's `asset_id`. Vendor IDs go in sidecar columns for reconciliation only.

4. **Targets are time-versioned.** Editing `targets.yaml` (or hitting `/targets`) closes the previous `target_allocation` rows (`effective_to = now`) and inserts new ones. Never UPDATE in place — that destroys history needed for honest review.

5. **Suggestions are first-class rows.** Every recommendation lands in `order_suggestion` with `status` (`pending` | `accepted` | `rejected` | `expired`). The accept/reject flow updates status only — it never calls a broker. The product's audit trail is "what I suggested vs. what actually filled."

6. **Storage philosophy: SQLite for OLTP, DuckDB for analytics, Parquet for bars, eventually Postgres for OLTP at scale.** Phase 0–4: SQLite for transactional tables, DuckDB (direct Python, not via SQLAlchemy) for Parquet-based analytical queries. Phase 5 (multi-user): split — Postgres for `users`, `target_allocation`, `order_suggestion`, `alert`; DuckDB/MotherDuck for `price_bar`, backtest results.

7. **Single writer (SQLite).** All writes go through the FastAPI process or one CLI script at a time. Don't run `uvicorn` and a manual sync script simultaneously against the same `investor.db` file.

8. **All timestamps are UTC at rest.** Convert to `America/New_York` only for display or for cron triggers.

9. **ORM objects never leave the service layer.** Convert SQLAlchemy model instances to plain frozen dataclasses before returning from any `services/` function. Templates, jobs, and API response builders must only ever see ordinary Python values — never ORM objects. This prevents SQLAlchemy's "detached instance" error, which occurs when a lazy-load is attempted after the session closes. The pattern:

   ```python
   # Inside compose_daily_report(), while session is still open:
   account = AccountSnapshot(
       broker=orm_row.broker,
       cash_usd=orm_row.cash_usd,
       ...
   )
   # Return the plain dataclass, not the ORM object
   ```

   `GapRow`, `AccountSnapshot`, `Position` (Phase 2+) all follow this pattern. If you find yourself passing an ORM model instance to a template or a job function, stop and introduce a frozen dataclass.

## Code style

- Strict mypy on `src/`. Keep it green.
- `ruff` rules: `E F I N B UP SIM`. Run `ruff check --fix` before commit.
- Public functions get docstrings; private (underscore-prefixed) don't need them.
- Prefer pure functions in `services/`. `jobs/` is the orchestration layer that opens sessions, calls services, handles errors.
- No `print()` in `src/` — use `logging.getLogger(__name__)`. `print()` is fine in `scripts/` and tests.

## Things to never do

- **Never call a broker's `submit_order` / equivalent from inside the app.** This is a hard product constraint, not a style preference. Only `submit_order_draft` or no order code at all is allowed in v1. If asked to "automate trades," push back and reference the suggest-only product principle.
- **Never commit `.env`, `data/*.db`, `data/*.duckdb`, or any file under `data/`.** The `.gitignore` covers this; don't override.
- **Never let the LLM emit price targets, fundamental claims, or trade recommendations.** The LLM's allowed outputs are: news summaries, "is this material?" classification, and structured JSON labels (bullish/bearish/neutral). If a prompt would produce more than that, restrict it.
- **Never make `BrokerAdapter` async without an ADR.** APScheduler jobs and FastAPI endpoints are both happy with sync; mixing async/sync brokers complicates testing without buying anything.
- **Never store secrets in the database.** Keys live in `.env` only. Phase 5 introduces envelope-encrypted credential storage when needed.
- **Never silently UPDATE a `target_allocation` row.** Use the time-versioned close-and-insert pattern.
- **Never let LLM output flow into the suggestion engine without schema validation and explicit deterministic fallback.** If `score_levels_for_ticker()` fails or returns `[]`, `generate_suggestions()` falls back to Phase 2 nearest-distance logic automatically. Do not short-circuit this fallback.
- **Never mutate LangGraph node state in place.** Always return `{**state, "new_key": value}`. Mutations work in tests but break checkpointing.
- **Never call `session.commit()` inside a LangGraph node.** The `SqliteSaver` checkpointer and the OLTP engine share the SQLite write lock; committing inside a node causes deadlock. Persist after `graph.invoke()` returns.
- **Never authenticate the Agent SDK with consumer OAuth tokens for automated/unattended use.** Anthropic's ToS prohibits using consumer Claude.ai OAuth for automated scripts. `ANTHROPIC_API_KEY` is always required, even when `LLM_BACKEND=agent_sdk`.

## Common gotchas

1. **SQLite write contention.** SQLite serialises writes; if you run a CLI script while uvicorn is up, they share the same file and will queue (not deadlock). Avoid running both simultaneously for heavy writes.
2. **Alpaca paper account is initially empty.** Place a few paper trades in the Alpaca dashboard before testing the gap query — otherwise every ticker shows 100% gap and you can't tell if the math works.
3. **Cash buffer creates a permanent under-target.** If your YAML targets sum to 100 but you keep a 5% cash buffer, every ticker will look 0.5% under-target. Either set targets to sum to 95 (and accept they show "on target" against equity-only weights), or scale the gap by `equity / (equity - cash_buffer)` in the SQL. Document the choice in an ADR.
4. **Moomoo OpenD is a host-side dependency, not a containerised service.** OpenD runs on macOS/Windows; in Docker the app reaches it via `host.docker.internal:11111`. Do not try to install OpenD inside the app image.
5. **`alpaca-py` returns strings for numeric fields.** Wrap in `float()` at the adapter boundary.
6. **APScheduler timezones.** `BackgroundScheduler(timezone="America/New_York")` is the right default for cron triggers, but `DateTrigger(run_date=...)` interprets naive datetimes in scheduler-local time — pass `datetime.now(UTC)` explicitly to avoid surprises.
7. **Alembic batch mode for SQLite column changes.** SQLite can't `ALTER COLUMN` or `DROP COLUMN` directly. Use `render_as_batch=True` (already set in `migrations/env.py`) — Alembic recreates the table transparently.
8. **HMAC secret rotation invalidates all magic links already in inboxes.** If you rotate `MAGIC_LINK_SECRET`, any Accept/Reject links from the previous weekly email will return 400. Rotate only after the current week's suggestions have been acted on or expired.
9. **LangGraph/LangChain version pinning.** Pin `langgraph`, `langchain-core`, `langchain-anthropic`, `langgraph-checkpoint-sqlite` to specific minor versions; the ecosystem is historically version-churn-prone. The graph-integration test in `test_news_triage.py` is the canary after any upgrade.
10. **News URL normalization.** Alpaca and Finnhub serve the same Benzinga articles with different `?utm_source=` params. `_normalise_url()` in `services/news.py` strips query params and lowercases the host before hashing — this is what prevents double-insertion into `news_event`.
11. **Movers on holidays.** A ≥5% vs. last-week-close on Monday after a 3-day weekend can be noisy ("last week" lands inside the holiday window). Acceptable degradation; documented.
12. **`SqliteSaver.from_conn_string` is a `@contextmanager`.** It cannot be used at module level. Use `SqliteSaver(sqlite3.connect(db_path, check_same_thread=False))` directly, as done in `graphs/__init__.py:make_checkpointer()`.
13. **`AgentSDKClient.call()` uses `asyncio.run()` — APScheduler-safe, async-route-unsafe.** The sync bridge is fine in APScheduler job threads (each has no live event loop). Do NOT call `llm.call()` on an `AgentSDKClient` from inside an `async def` FastAPI route — it will raise `RuntimeError: This event loop is already running`.
14. **`LLM_BACKEND` env var defaults to `anthropic_api`.** Set to `agent_sdk` to route LLM calls through `claude-agent-sdk`. Unknown values fall back to `anthropic_api` with a warning log. `LLMClient` is now a Protocol (`services/llm.py`); use `make_llm_client(settings)` factory, not `LLMClient(...)` directly. See ADR-0016.

## Required env vars

See `.env.example` for the canonical list. The app **fails to start** if any of these are missing or invalid:

- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (when `BROKER` starts with `alpaca`)
- `SQLITE_PATH`
- `TARGETS_PATH`

Phase 1+ adds: `SMTP_*`, `EMAIL_*`, `ANTHROPIC_API_KEY`. They're declared as Optional in `config.py` but the relevant features won't work without them.

Phase 3a adds: `ANTHROPIC_API_KEY` (Sonnet scoring), `MAGIC_LINK_SECRET` (HMAC for email buttons, distinct from ADMIN_TOKEN), `APP_BASE_URL` (magic-link URL base).

Phase 3b adds: `FINNHUB_API_KEY` (Finnhub free tier; optional but needed as Alpaca fallback). `LLM_DAILY_COST_CAP_USD=3.0` (updated from 1.0 in Phase 3a). `LLM_BACKEND=anthropic_api` (or `agent_sdk` to route through `claude-agent-sdk`; see ADR-0016).

## Where to find more

- **Product plan:** `product_plan.md` — vision, phases, broker comparison, cost.
- **Phase guides:** `phase_0_guide.md` (current), `phase_N_guide.md` as added.
- **ADRs:** `docs/adr/0001-*.md`, `0002-*.md`, …
- **Open questions:** §7 of `product_plan.md`. If you're about to make an irreversible choice, check there first.
