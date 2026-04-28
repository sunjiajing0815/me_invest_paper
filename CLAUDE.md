# Investor Assistant — CLAUDE.md

This file orients Claude Code (and any future LLM agents) to this repository. Read it before making changes.

## Mission

A self-hosted, suggest-only assistant for a long-term US-equities investor. The system pulls positions from a broker (Alpaca first, Moomoo later), compares against a YAML-defined target allocation, identifies support/resistance levels, suggests weekly orders, and emails daily/weekly reports. **The system never places orders.** Order execution is always manual, in the broker's own UI.

Owner: Jane (solo developer, primary user). Multi-tenant productization is Phase 5 — until then, treat this as a single-user app.

## Current phase

See `docs/phase_0_guide.md` (or the project-root copy) for the active build plan. The repo's git tag (e.g., `v0.0.1-phase-0`) reflects the last completed phase.

## Tech stack and why

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.13 | Best fintech/ML ecosystem |
| API | FastAPI | Lifespan hooks for scheduler bootstrap; OpenAPI for free |
| Scheduler | APScheduler (in-process) | No Redis/Celery needed at single-user scale |
| DB | DuckDB via `duckdb-engine` + SQLAlchemy + Alembic | Vectorized analytics on OHLCV; single file; window functions |
| Bars (cold storage) | Parquet under `data/bars/` | Survives DB migrations; queryable directly by DuckDB |
| Broker (v1) | Alpaca via `alpaca-py` | REST, no gateway, free paper, fractional shares, AU residents allowed |
| Broker (later) | Moomoo via OpenD on host | Already-funded account for actual long-term capital |
| LLM | Anthropic Claude (Haiku 4.5 triage, Sonnet 4.6 review) | Used in Phase 3+ for news triage |
| Email | SMTP via Gmail App Password | Phase 1+; plain `smtplib` + Jinja2 templates |
| Container | Single Dockerfile, one `docker-compose.yml` | Same image runs on Mac (Docker Desktop) or VPS |
| Package manager | `uv` | Fast, single source of truth via `pyproject.toml` |

## Repo layout

```
src/investor/
  main.py             FastAPI app + lifespan
  config.py           pydantic-settings + targets.yaml loader
  db.py               DuckDB engine + session factory
  models.py           SQLAlchemy ORM models
  scheduler.py        APScheduler bootstrap
  brokers/
    base.py           BrokerAdapter Protocol + dataclasses
    alpaca.py         AlpacaAdapter
    moomoo.py         (future) MoomooAdapter — talks to OpenD on host
  services/           pure functions: snapshot, gap, levels, suggest, news
  jobs/               APScheduler-registered job functions; thin wrappers over services
config/targets.yaml   user's target allocation (hand-edited or via /targets API)
migrations/           Alembic revisions
scripts/              standalone CLIs (sync_positions, show_gap, load_targets)
docs/adr/             Architecture Decision Records, numbered 0001+
data/                 DuckDB file + Parquet bars; bind-mounted; gitignored
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

6. **Storage philosophy: DuckDB for analytics, Parquet for bars, eventually Postgres for OLTP.** Phase 0–4: everything in DuckDB. Phase 5 (multi-user): split — Postgres for `users`, `target_allocation`, `order_suggestion`, `alert`; DuckDB/MotherDuck for `price_bar`, `positions_snapshot`, backtest results.

7. **Single writer.** While in single-DuckDB-file mode, all writes go through the FastAPI process or one CLI script at a time. Don't run `uvicorn --reload` and a manual sync script simultaneously — DuckDB will lock.

8. **All timestamps are UTC at rest.** Convert to `America/New_York` only for display or for cron triggers.

## Code style

- Strict mypy on `src/`. Keep it green.
- `ruff` rules: `E F I N B UP SIM`. Run `ruff check --fix` before commit.
- Public functions get docstrings; private (underscore-prefixed) don't need them.
- Prefer pure functions in `services/`. `jobs/` is the orchestration layer that opens sessions, calls services, handles errors.
- No `print()` in `src/` — use `logging.getLogger(__name__)`. `print()` is fine in `scripts/` and tests.

## Things to never do

- **Never call a broker's `submit_order` / equivalent from inside the app.** This is a hard product constraint, not a style preference. Only `submit_order_draft` or no order code at all is allowed in v1. If asked to "automate trades," push back and reference the suggest-only product principle.
- **Never commit `.env`, `data/*.duckdb`, or any file under `data/`.** The `.gitignore` covers this; don't override.
- **Never let the LLM emit price targets, fundamental claims, or trade recommendations.** The LLM's allowed outputs are: news summaries, "is this material?" classification, and structured JSON labels (bullish/bearish/neutral). If a prompt would produce more than that, restrict it.
- **Never make `BrokerAdapter` async without an ADR.** APScheduler jobs and FastAPI endpoints are both happy with sync; mixing async/sync brokers complicates testing without buying anything.
- **Never store secrets in the database.** Keys live in `.env` only. Phase 5 introduces envelope-encrypted credential storage when needed.
- **Never silently UPDATE a `target_allocation` row.** Use the time-versioned close-and-insert pattern.

## Common gotchas

1. **DuckDB write locks.** If you see `IO Error: Could not set lock on file`, another process is holding the DB file. Stop the server or the script, then retry.
2. **Alpaca paper account is initially empty.** Place a few paper trades in the Alpaca dashboard before testing the gap query — otherwise every ticker shows 100% gap and you can't tell if the math works.
3. **Cash buffer creates a permanent under-target.** If your YAML targets sum to 100 but you keep a 5% cash buffer, every ticker will look 0.5% under-target. Either set targets to sum to 95 (and accept they show "on target" against equity-only weights), or scale the gap by `equity / (equity - cash_buffer)` in the SQL. Document the choice in an ADR.
4. **Moomoo OpenD is a host-side dependency, not a containerised service.** OpenD runs on macOS/Windows; in Docker the app reaches it via `host.docker.internal:11111`. Do not try to install OpenD inside the app image.
5. **`alpaca-py` returns strings for numeric fields.** Wrap in `float()` at the adapter boundary.
6. **APScheduler timezones.** `BackgroundScheduler(timezone="America/New_York")` is the right default for cron triggers, but `DateTrigger(run_date=...)` interprets naive datetimes in scheduler-local time — pass `datetime.now(UTC)` explicitly to avoid surprises.
7. **`alembic --autogenerate` and DuckDB.** Works, but doesn't always detect column-type changes. When in doubt, write the migration by hand.

## Required env vars

See `.env.example` for the canonical list. The app **fails to start** if any of these are missing or invalid:

- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (when `BROKER` starts with `alpaca`)
- `DUCKDB_PATH`
- `TARGETS_PATH`

Phase 1+ adds: `SMTP_*`, `EMAIL_*`, `ANTHROPIC_API_KEY`. They're declared as Optional in `config.py` but the relevant features won't work without them.

## Where to find more

- **Product plan:** `product_plan.md` — vision, phases, broker comparison, cost.
- **Phase guides:** `phase_0_guide.md` (current), `phase_N_guide.md` as added.
- **ADRs:** `docs/adr/0001-*.md`, `0002-*.md`, …
- **Open questions:** §7 of `product_plan.md`. If you're about to make an irreversible choice, check there first.
