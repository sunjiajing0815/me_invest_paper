# Investor Assistant — CLAUDE.md

This file orients Claude Code (and any future LLM agents) to this repository. Read it before making changes.

## Mission

A self-hosted, suggest-only assistant for a long-term US-equities investor. The system pulls positions from one or more broker accounts per user (Alpaca + Moomoo as of Phase 4.9a; IBKR + Tiger are deferred follow-ons), compares against a YAML-defined target allocation, identifies support/resistance levels, suggests weekly orders, and emails daily/weekly reports **per broker account**. **The system never places orders.** Order execution is always manual, in the broker's own UI.

Owner: Jane (solo developer, primary user). Multi-tenant productization is Phase 5 — until then, treat this as a single-user app.

## Current phase

Phase 4 code is complete — tagged `v0.4.8-phase-4-code-complete`. Phase 4.9a (multi-broker plumbing + per-broker reports) Stage A–C is code-complete on `main`; remaining before the `v0.4.9a.0` tag: the 2-broker smoke test. See `plans/phase_4_9a_guide.md` and `plans/phase_4_9a_completion.md`.

Active phases: 0 (foundation), 1 (daily email + bar backfill), 2 (indicators, S/R levels, weekly order suggestions), 3 (LLM-scored levels, news triage, suggestion review pipeline), 4 (reconciliation, Moomoo adapter, weekly review, opt-in auto-trade).

Phase 4 promotion soak stages (all require manual promotion via `POST /admin/auto-trade/promote`):
- `v0.4.1-paper-dry-run` — DRY_RUN on Alpaca paper, clean for 2 weeks
- `v0.4.2-paper-live` — LIVE on Alpaca paper, clean for 4 weeks
- `v0.4.3-alpaca-live` — LIVE on real Alpaca, clean for 4 weeks
- `v0.4.4-moomoo-live` — LIVE on Moomoo after parallel-run + primary flip + 4 weeks

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
    __init__.py       make_adapter(settings) [primary] + make_account_adapter(broker, connection_config) + build_account_adapters(session) → {account_ref: adapter} (Phase 4.9a)
    base.py           BrokerAdapter Protocol + dataclasses
    alpaca.py         AlpacaAdapter
    moomoo.py         MoomooAdapter — talks to OpenD on host (Phase 4)
  services/
    accounts.py       AccountInfo + list_active_accounts / resolve_primary_account_ref / resolve_active_account_refs (Phase 4.9a multi-broker)
    snapshot.py       position + account ingestion (per broker_account_id)
    gap.py            target-vs-actual gap computation (per broker_account_id)
    analytics.py      DuckDB context manager (price_bar view over Parquet)
    indicators.py     IndicatorRow + compute_indicators() — SMA/EMA/RSI/MACD
    levels.py         SRLevelRow + compute_levels() / persist_levels() / build_nearby_levels()
    suggest.py        OrderSuggestionRow + generate_suggestions() / persist_suggestions()
    daily_report.py   DailyReport dataclass + compose_daily_report()
    bars.py           update_bars() — Alpaca IEX → Parquet append
    targets.py        load_targets_into_db()
    render.py         Jinja2 template rendering
    email.py          SMTPEmailer + FakeEmailer
    reconciliation.py MatchResult + reconcile_activities() / persist_reconciliation() / compute_realized_pnl()
    auto_trade.py     AutoTradeOutcome + run_auto_trade_pass() + guards + _trigger_kill_switch()
    tavily.py         TavilyClient Protocol + TavilyConcreteClient + FakeTavilyClient + make_tavily_client()
    weekly_context.py WeeklyMarketContext + build_weekly_market_context() — Tavily fanout + Sonnet synthesis
  jobs/
    daily_report.py   Mon-Fri 16:15 ET — sync, indicators, compose, email
    weekly_suggestions.py  Sun 18:00 ET — indicators, levels, suggestions, email
    reconciliation.py Mon-Fri 16:45 ET — match broker fills to suggestions
    moomoo_parallel.py  Mon-Fri 16:50 ET — compare Moomoo vs Alpaca (parallel-run soak)
    weekly_review.py  Fri 17:00 ET — 8-section reflection email (section 7 = Tavily market context)
    auto_trade.py     Mon-Fri 09:35 ET — place orders for accepted suggestions
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

3. **Domain IDs ≠ broker IDs.** Per-account tables key on `(broker_account_id, ticker)` (Phase 4.9a) — eventually `(user_id, broker_account_id, ticker)` in Phase 5a — never on the broker's `asset_id`. `broker_account_id` is the stable `broker_account.account_ref` partition key (a plain column, no DB FK — app-enforced; see ADR-0024). News, S/R levels, and market context stay **user-level**, not per-account. Vendor IDs go in sidecar columns for reconciliation only.

4. **Targets are time-versioned.** Editing `targets.yaml` (or hitting `/targets`) closes the previous `target_allocation` rows (`effective_to = now`) and inserts new ones. Never UPDATE in place — that destroys history needed for honest review.

5. **Suggestions are first-class rows.** Every recommendation lands in `order_suggestion` with `status` (`pending` | `accepted` | `rejected` | `expired`). The accept/reject flow updates status only — it never calls a broker. The product's audit trail is "what I suggested vs. what actually filled."

6. **Storage philosophy: SQLite for OLTP, DuckDB for analytics, Parquet for bars, eventually Postgres for OLTP at scale.** Phase 0–4: SQLite for transactional tables, DuckDB (direct Python, not via SQLAlchemy) for Parquet-based analytical queries. Phase 5 (multi-user): split — Postgres for `users`, `target_allocation`, `order_suggestion`, `alert`; DuckDB/MotherDuck for `price_bar`, backtest results.

7. **Single writer (SQLite).** All writes go through the FastAPI process or one CLI script at a time. Don't run `uvicorn` and a manual sync script simultaneously against the same `investor.db` file.

8. **All timestamps are UTC at rest.** Convert to `America/New_York` only for display or for cron triggers.

9. **ORM objects never leave the service layer — and never outlive their session.** Convert SQLAlchemy model instances to plain Python values (frozen dataclasses, dicts, primitives) *before* the `session_scope()` block closes. This rule has three distinct callsites and has caused a `DetachedInstanceError` at each of them on three separate occasions:

   - Phase 1: `BrokerAccount` ORM object accessed after session closed in `compose_daily_report()`
   - Phase 3b: `MoverState` ORM objects stored in a dict, session closed, attributes accessed outside
   - The fix in both cases is identical — extract the values you need *inside* the session, not outside.

   The canonical pattern in a LangGraph graph is a dedicated **`gather_context_node`** that runs first, opens a session, reads all needed ORM data into a frozen `GraphContext` dataclass, closes the session, and puts the dataclass on graph state. All downstream LLM nodes receive the frozen dataclass — they never touch a session. This is the only correct pattern for passing database data into a graph.

   ```python
   # gather_context_node — runs first, session closes before any LLM node
   with session_scope() as s:
       ctx = GraphContext(
           gap_rows=[GapRow(...) for row in s.query(...)],
           last_triggered=s.query(MoverState).filter_by(ticker=ticker).one_or_none(),
           # extract all scalar/dataclass values here
       )
   return {**state, "context": ctx}   # frozen dataclass on state, session gone
   ```

   If you find yourself accessing an ORM attribute outside a `with session_scope()` block, or passing a live ORM object into a LangGraph node, stop and apply the `gather_context_node` pattern.

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
- **Never call `session.commit()` inside a LangGraph node.** Persist after `graph.invoke()` returns. The checkpointer (`MemorySaver`) and the OLTP session must not compete for the write lock — keep them in separate scopes.
- **Never authenticate the Agent SDK with consumer OAuth tokens for automated/unattended use.** Anthropic's ToS prohibits using consumer Claude.ai OAuth for automated scripts. `ANTHROPIC_API_KEY` is always required, even when `LLM_BACKEND=agent_sdk`.
- **Never make the `revise_node` LLM-driven — LLMs propose changes, Python applies them.** The `revise_node` in `graphs/suggestion_review.py` is intentionally deterministic Python: `_apply_changes()` validates every critic-proposed change against known scored levels and rejects invented prices or unknown methods. A second LLM-driven revision would add hallucination risk, create loop risk (the critic might then revise its own revision), and add cost with no benefit. See ADR-0013.
- **Never extend `LLM_BACKEND=agent_sdk` consumer-OAuth login into multi-user deployment.** The current single-user setup is personal automation (permitted by Anthropic's ToS). A shared consumer OAuth session across multiple users violates ToS. Phase 5 multi-tenant must use individual API keys per user.
- **Never feed Tavily results into the suggestion engine or order-execution path — with one bounded exception.** Tavily output flows into `WeeklyMarketContext` and from there into the email template AND the `context_adjust_node` size multiplier (Phase 4.7). The exception is strictly bounded: `context_adjust_node` may scale suggestion *quantities* only, within Python-clamped `[context_size_min, context_size_max]`, using only existing scored S/R level anchors. It must never reach `generate_suggestions()`, `run_auto_trade_pass()`, or any broker adapter. See ADR-0020, ADR-0021.

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
12. **LangGraph checkpointer is `MemorySaver`, not `SqliteSaver`.** Phase 3b discovered that `SqliteSaver` causes `database is locked` errors: when a graph node calls `session.flush()` for `llm_call_log`, SQLAlchemy starts a write transaction; `SqliteSaver`'s separate `sqlite3` connection then can't acquire the write lock to checkpoint between nodes. `MemorySaver` (in-memory, per-`graph.invoke()`) eliminates the contention entirely. Graph checkpoint state is ephemeral — one `invoke()` per ticker per run — so disk persistence is not needed. Do not revert to `SqliteSaver`. `langgraph --thread-id` trace inspection does not work with `MemorySaver`; use logging inside nodes instead.
13. **`AgentSDKClient.call()` uses `asyncio.run()` — APScheduler-safe, async-route-unsafe.** The sync bridge is fine in APScheduler job threads (each has no live event loop). Do NOT call `llm.call()` on an `AgentSDKClient` from inside an `async def` FastAPI route — it will raise `RuntimeError: This event loop is already running`.
14. **`LLM_BACKEND` env var defaults to `anthropic_api`.** Set to `agent_sdk` to route LLM calls through `claude-agent-sdk`. Unknown values fall back to `anthropic_api` with a warning log. `LLMClient` is now a Protocol (`services/llm.py`); use `make_llm_client(settings)` factory, not `LLMClient(...)` directly. See ADR-0016.
16. **Moomoo OpenD bind address must be `0.0.0.0:11111`, not `127.0.0.1`.** A loopback-bound OpenD is unreachable from Docker. Verify with `lsof -i :11111` on the host.
17. **Moomoo ticker prefix stripping is enforced at the adapter boundary.** `_strip_market_prefix()` in `brokers/moomoo.py` converts `US.AAPL` → `AAPL`. No ticker with a market prefix should ever appear outside that file. Similarly, `submit_order()` adds `US.` internally — callers always use bare tickers.
18. **`client_order_id` ↔ `remark` mapping in Moomoo.** Moomoo has no native `client_order_id` field. The adapter stores it in the `remark` field on `place_order()` and reads it back from `remark` in `deal_list_query()` and `order_list_query()`. Callers see `Activity.client_order_id` — the `remark` mapping is invisible outside the adapter (per ADR-0018).
19. **Wash-sale window is 30 calendar days, not trading days.** The guard checks `filled_at >= now - timedelta(days=30)`. It only applies to real fills (`dry_run=False`). A `dry_run=True` simulated loss sell does NOT trigger the wash-sale guard for subsequent real buys — the `dry_run.is_(False)` filter in `_check_wash_sale()` is intentional and critical.
20. **`dry_run=False` filter is mandatory in every reconciliation and auto-trade query.** Simulated losses from `DRY_RUN` mode must never interfere with real PnL accounting, wash-sale guards, or cap calculations. If you add a new query that touches `order_execution`, include `OrderExecution.dry_run.is_(False)` unless you explicitly want DRY_RUN rows too.
15. **Phase 3c adds three new prompt files** under `src/investor/prompts/`: `suggestion_reason_v1.txt` (Sonnet per-draft rationale system prompt), `suggestion_critic_v1.txt` (Sonnet cross-suggestion critic system prompt with five severity-ordered criteria), and `score_levels_v2.txt` (news-augmented copy of `score_levels_v1.txt`). The `level_prompt_version` setting (default `"v2"`) selects the scoring prompt. The reason/critic prompts are always `v1` (no version setting). All prompts live in the `prompts/` directory and are loaded via `load_prompt()` in `services/prompts.py`.
16. **Reconciliation is matching, not creation.** `services/reconciliation.py` writes `order_execution` rows by matching broker activities to existing `order_suggestion` rows. It never invents executions. Four matching rules and priority order are fixed in ADR-0017. Every reconciliation and wash-sale query must include `WHERE dry_run = false` to prevent simulated DRY_RUN rows from interfering with real trade logic.
17. **Auto-trade mode defaults to OFF forever — now per broker account.** As of Phase 4.9a the mode lives in `auto_trade_state` keyed by `broker_account_id` (= `account_ref`), not the old `meta.auto_trade_mode` key (which migration `d8589` deletes). `_get_mode(session, broker_account_id)` falls back to `'OFF'` if the row is absent or unrecognised, and each broker promotes through its own OFF → DRY_RUN → LIVE soak ladder independently — promoting one broker never promotes another. Guards, caps, and the kill switch are all scoped per `broker_account_id`. New brokers (via `POST /admin/broker-accounts`) seed OFF. Promotion still requires `AUTO_TRADE_PROMOTION_TOKEN` + soak-window enforcement. See ADR-0014 and ADR-0024.
18. **`submit_order()` is a single-call-site privilege.** Only `services/auto_trade.py` and `brokers/` may call `adapter.submit_order()`. The grep CI test `tests/test_no_unauthorized_submit_order.py` enforces this on every run.
21. **Tavily acquired by Nebius (Feb 2026) — pin `tavily-python>=0.6,<0.7`.** The SDK is pre-1.0; Nebius ownership means the API surface may change. Tight pinning prevents silent breakage. Swap path if needed: create a new concrete client implementing `TavilyClient` Protocol and update `make_tavily_client()` factory — no call-site changes needed. See ADR-0020.
22. **Tavily monthly cap is per-instance, not persisted.** `TavilyConcreteClient._used_this_month` resets to 0 when the process restarts. If the app restarts mid-month, the counter resets. For conservative usage this is fine (weekly review adds ~12–16 searches/week × 4 = ~60/month well under the 200 default). Don't raise `TAVILY_MONTHLY_CAP` above the free-tier limit without checking Tavily's current pricing.
23. **Monday movers use a 48-hour news lookback.** `jobs/movers.py` widens the lookback from 24h to 48h specifically on Mondays (`datetime.weekday() == 0`) to catch Friday/weekend news that drives Monday moves. Other days use 24h.
24. **Week-of alignment for context_adjust_node (Friday→Sunday bridge).** `run_weekly_review` persists market context with `week_of=_next_monday()` (the upcoming Monday). Sunday's `gather_context_node` loads with `state["week_of"]` which is also the upcoming Monday. They must match — do not use `week_of - 7 days` as the persist key.
25. **Stale context is silently skipped.** If `load_latest_weekly_context` finds no row within `context_max_age_days=4` for the upcoming Monday, it returns `None` and `context_adjust_node` skips the narrative pass entirely. The earnings gate still runs independently. No error is raised — check logs for "no fresh context" if the narrative pass seems absent.
26. **`context_adjust_node` earnings gate uses Finnhub, not Tavily `forward_events`.** The earnings gate calls `earnings_client.upcoming_earnings()` (Finnhub-backed). If `FINNHUB_API_KEY` is empty, `make_earnings_client()` returns a `FakeEarningsClient(_canned={})` and the gate is a no-op with a WARNING log. Do not wire the earnings gate to Tavily's `forward_events` free-text field.

## Required env vars

See `.env.example` for the canonical list. The app **fails to start** if any of these are missing or invalid:

- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (when `BROKER` starts with `alpaca`)
- `SQLITE_PATH`
- `TARGETS_PATH`

Phase 1+ adds: `SMTP_*`, `EMAIL_*`, `ANTHROPIC_API_KEY`. They're declared as Optional in `config.py` but the relevant features won't work without them.

Phase 3a adds: `ANTHROPIC_API_KEY` (Sonnet scoring), `MAGIC_LINK_SECRET` (HMAC for email buttons, distinct from ADMIN_TOKEN), `APP_BASE_URL` (magic-link URL base).

Phase 3b adds: `FINNHUB_API_KEY` (Finnhub free tier; optional but needed as Alpaca fallback). `LLM_DAILY_COST_CAP_USD=3.0` (updated from 1.0 in Phase 3a). `LLM_BACKEND=anthropic_api` (or `agent_sdk` to route through `claude-agent-sdk`; see ADR-0016).

Phase 4 adds: `OPEND_HOST=host.docker.internal`, `OPEND_PORT=11111`, `OPEND_SECURITY_FIRM=FUTUSECURITIES` (Moomoo/Futu OpenD daemon settings — only needed when `BROKER=moomoo`). `AUTO_TRADE_PROMOTION_TOKEN` — separate from `ADMIN_TOKEN`; required for auto-trade mode promotions.

Phase 4.9a (multi-broker) adds no required env vars. Per-broker credentials/connection params live in each `broker_account.connection_config` JSON blob (which names env-var keys for the adapter factory to resolve). Targets are now per broker account: `data/targets/<broker_account_id>.yaml`, with the primary account falling back to `TARGETS_PATH` (`config/targets.yaml`) for one release. Connect a broker with `POST /admin/broker-accounts`; account-scoped endpoints take an optional `?broker_account_id` (default: primary for reads, all-active for job triggers/bulk mutations).

Phase 4.5 adds: `TAVILY_API_KEY` (optional; empty = graceful skip of weekly market context section), `TAVILY_MONTHLY_CAP=200` (default 200; cap reached → silent empty + WARNING log).

Phase 4.7 adds: `FINNHUB_API_KEY` (already in config since Phase 3b; now also used for the earnings gate in `context_adjust_node`; empty = no-op gate + WARNING). New sizing settings — all have defaults and are optional: `EARNINGS_SIZE_FACTOR=0.5`, `EARNINGS_REANCHOR=true`, `EARNINGS_LOOKAHEAD_DAYS=7`, `CONTEXT_SIZE_MIN=0.25`, `CONTEXT_SIZE_MAX=1.5`, `CONTEXT_MAX_AGE_DAYS=4`, `CONTEXT_ADJUST_PROMPT_VERSION=v1`, `CRITIC_PROMPT_VERSION=v2`.

## Where to find more

- **Product plan:** `product_plan.md` — vision, phases, broker comparison, cost.
- **Phase guides:** `phase_0_guide.md` (current), `phase_N_guide.md` as added.
- **ADRs:** `docs/adr/0001-*.md`, `0002-*.md`, …
- **Open questions:** §7 of `product_plan.md`. If you're about to make an irreversible choice, check there first.
