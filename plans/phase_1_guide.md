# Phase 1 — Portfolio & Gap: Step-by-Step Guide

**Goal:** End with a reliable daily email landing in your inbox at ~16:30 ET on every trading day, containing your current allocation, gap vs. target, and drift-band flags. By end of Phase 1 you also have 2 years of OHLCV bars on disk ready for Phase 2's indicator work, and the technical-debt items carried out of Phase 0 are resolved.

**Out of scope for Phase 1:** support/resistance levels, weekly order suggestions, news, LLM, web UI, accept/reject buttons. (All Phase 2–4.)

**Time budget:** 5–7 evenings (15–20 focused hours).

**Definition of done:** all 14 smoke-test rows below pass, *and* you've received 5 consecutive scheduled daily emails on real trading days without manual intervention.

---

## 0. Pre-flight checklist

These unblock everything else. ~20 minutes.

- [ ] Gmail App Password generated at `https://myaccount.google.com/apppasswords` (you can't use your normal Gmail password — Google killed that years ago).
- [ ] `.env` updated: `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=<your-gmail>@gmail.com`, `SMTP_APP_PASSWORD=<the-16-char-token>`, `EMAIL_FROM=<your-gmail>@gmail.com`, `EMAIL_TO=<your-personal-email>` (can be the same gmail).
- [ ] `chmod 600 .env` (in case you forgot in Phase 0).
- [ ] Phase 0 smoke test still passes (regression check before adding new code): `curl localhost:8000/health` returns 200 with non-null `last_sync_ts`.
- [ ] Place 3–5 paper trades in the Alpaca dashboard if you haven't — Phase 1's email needs non-trivial data to validate against.
- [ ] Bump version: `pyproject.toml → 0.1.0` and decide on Python — pin **3.12** in `pyproject.toml` and `Dockerfile` (the `python:3.13-slim` runtime is fine but introduces drift; pick one and align CLAUDE.md, Dockerfile, and `pyproject.toml`).

---

## 1. Resolve Phase 0 carryovers first

Before adding new features, clear technical debt. Two evenings of unsexy work that pays back ten-fold by Phase 4.

### 1a. ADR-0002 — Alembic vs. inline migrations

The Phase 0 agent skipped Alembic and rolled `ALTER TABLE … ADD COLUMN IF NOT EXISTS` inside `init_db()`. That works for adding columns but cannot handle column renames, type changes, dropped tables, foreign keys, or rollbacks. Phases 2–4 will introduce all of those.

Write `docs/adr/0002-schema-migrations.md`. Recommended decision: **adopt Alembic now, batch_alter_table mode for DuckDB.** Concretely:

```bash
uv run alembic init migrations
```

Edit `migrations/env.py` to read `DUCKDB_PATH` from the same Settings object the app uses. Then **stamp** the existing schema as the baseline (don't try to autogenerate over data you already have):

```bash
uv run alembic revision --autogenerate -m "phase0 baseline schema"
# inspect the generated file and DELETE all its op.create_table calls
# replace with: pass  (it's a no-op marker; the schema already exists)
uv run alembic stamp head
```

From now on:

```bash
uv run alembic revision --autogenerate -m "phase1 add price_bar"
uv run alembic upgrade head
```

Replace the `ALTER TABLE … ADD COLUMN IF NOT EXISTS` calls in `init_db()` with a single `command.upgrade(alembic_cfg, "head")` call at startup. The on-disk schema stays in sync without bespoke logic.

Watchpoints with DuckDB + Alembic: column-type changes need `with op.batch_alter_table(...)` even though DuckDB isn't SQLite — Alembic's batch mode is the universal escape hatch. Test by writing a throwaway migration that adds and immediately drops a column — verify it round-trips on a copy of your live `data/investor.duckdb`.

If you genuinely have evidence Alembic doesn't work for DuckDB in your specific case, the ADR should document **what you tried, what failed, and the exact error** — not a vague "DDL quirks." Without that evidence, take the deal.

### 1b. Fix `load_targets.py` dedup

The intermittent duplicate-row bug. Three changes, in this order:

1. **Stop running it on every container start.** Remove the `load_targets.py` invocation from the Dockerfile `CMD`. Move it to a one-time bootstrap step (`docker compose run app uv run python scripts/load_targets.py`) or to a new `POST /admin/reload-targets` endpoint that you hit manually after editing `targets.yaml`.

2. **Hash-based change detection.** Instead of comparing floats, hash the YAML content:

   ```python
   import hashlib, yaml
   yaml_text = Path(settings.targets_path).read_text()
   yaml_hash = hashlib.sha256(yaml_text.encode()).hexdigest()
   # store in a `meta` table: key='targets_yaml_hash', value=yaml_hash
   # only reload if hash changed
   ```

   This is bulletproof and faster than re-parsing + comparing. Float comparison should never have been load. The decision criterion.

3. **Regression test** — `tests/test_load_targets.py`:

   ```python
   def test_load_targets_idempotent(tmp_path):
       db = tmp_path / "x.duckdb"
       loader.run(db, fixture_yaml)
       loader.run(db, fixture_yaml)        # second call, no change
       loader.run(db, fixture_yaml)        # third call, no change
       open_rows = q("SELECT * FROM target_allocation WHERE effective_to IS NULL")
       assert len(open_rows) == EXPECTED_TICKER_COUNT

   def test_load_targets_versions_changes(tmp_path):
       db = tmp_path / "x.duckdb"
       loader.run(db, fixture_yaml_v1)
       loader.run(db, fixture_yaml_v2)
       open_rows = q("... effective_to IS NULL")
       closed_rows = q("... effective_to IS NOT NULL")
       assert len(closed_rows) == V1_TICKER_COUNT
       assert open_rows match v2
   ```

### 1c. `ALPACA_BASE_URL` cleanup

Either **delete the env var** (recommended — `paper=True` is the source of truth) or **actually thread it through** to `TradingClient(url_override=...)` for users on a custom proxy. Don't leave a stored-but-ignored variable; that's how production incidents start.

### 1d. Python version alignment

Pick 3.12 or 3.13 and apply consistently to: `pyproject.toml` (`requires-python`), `Dockerfile` (`FROM python:3.x-slim`), and CLAUDE.md. Recommendation: **3.12** — it's the LTS-flavoured choice for fintech libraries through end of 2026 and what every cloud Python image defaults to.

---

## 2. Recurring daily sync

Replace the one-shot `DateTrigger` in `src/investor/scheduler.py` with a recurring `CronTrigger`:

```python
from apscheduler.triggers.cron import CronTrigger

sched.add_job(
    run_daily_report,        # not run_sync_once — see §5
    trigger=CronTrigger(
        day_of_week="mon-fri",
        hour=16, minute=15,
        timezone="America/New_York",
    ),
    id="daily_report",
    replace_existing=True,
    misfire_grace_time=60 * 30,   # if the box was offline at 16:15, run within 30 min of restart
)
```

Why **16:15 ET**: Alpaca's daily bar prints around 16:00 close; the 15-minute buffer absorbs feed lag. If you observe the bar isn't reliably there at 16:15, push to 16:30.

Keep the `POST /admin/run-sync` and add `POST /admin/run-daily-report` for manual triggers — invaluable during development and when you're debugging an email at 11pm.

---

## 3. Email service

`src/investor/services/email.py`:

```python
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Protocol

class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, html: str, text: str) -> None: ...

class SMTPEmailer:
    def __init__(self, host: str, port: int, user: str, app_password: str):
        self._host, self._port = host, port
        self._user, self._password = user, app_password

    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, self._user, to
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(self._host, self._port) as smtp:
            smtp.starttls()
            smtp.login(self._user, self._password)
            smtp.sendmail(self._user, [to], msg.as_string())

class FakeEmailer:
    """Records sends in memory. Used in tests."""
    def __init__(self): self.sent = []
    def send(self, **kwargs): self.sent.append(kwargs)
```

The `EmailSender` Protocol + `FakeEmailer` lets you write fast tests for the daily-report job without a network round-trip. `FakeEmailer` is the single most important piece of test infrastructure you'll add this phase.

Wire `SMTPEmailer` in `main.py`'s lifespan and stash it on `app.state` so jobs can grab it.

---

## 4. Email templates

Create `templates/daily_report.html.j2` and `templates/daily_report.txt.j2`. Email clients are hostile — keep it simple:

- **No external CSS** (Gmail strips `<style>` tags in some views). Inline styles only.
- **No external images.** Inline as base64 data URIs or skip images entirely.
- **Tables for layout.** Yes, like 2003. Email clients still render `<table>` more reliably than CSS grid.
- **A plain-text version is mandatory** — some clients display it, and spam filters check for its presence.

Sections:

1. Header — date, account name, total equity, cash, day-over-day change.
2. **Allocation table** — one row per held ticker, columns: ticker, qty, avg cost, market value, current %, target %, gap %, in-band? (✓ or ⚠️).
3. **Gap summary** — top 3 most under-weight, top 3 most over-weight (these are the ones likely to drive next week's orders).
4. **Drift alerts** — list of tickers outside their bands (separate from the table for emphasis).
5. Footer — "No orders are placed automatically. Log into Alpaca to act."

Develop the template by rendering to a `.html` file and previewing in a browser **before** wiring SMTP. Don't waste real sends on layout iteration.

---

## 5. Daily report composer

`src/investor/services/daily_report.py`:

```python
@dataclass(frozen=True)
class DailyReport:
    date: date
    account: BrokerAccount
    positions: list[PositionsSnapshotRow]
    gap_rows: list[GapRow]
    drift_alerts: list[GapRow]   # subset of gap_rows where band_status != 'in_band'

def compose_daily_report(session) -> DailyReport:
    """Pure function. Reads DB, returns dataclass. No I/O."""
    account = get_latest_account(session)
    positions = get_latest_positions(session)
    gap_rows = compute_gap(session)   # phase 0 already returns this
    drift_alerts = [r for r in gap_rows if r.band_status != "in_band"]
    return DailyReport(date=date.today(), account=account,
                       positions=positions, gap_rows=gap_rows,
                       drift_alerts=drift_alerts)
```

Pure function = unit-testable in milliseconds with a fixture DB.

---

## 6. Drift band detection

Extend `GapRow` (in `services/gap.py`) with a `band_status: Literal["under", "in_band", "over"]` field. The SQL becomes:

```sql
SELECT
  t.ticker,
  COALESCE(c.weight_pct, 0) AS current_pct,
  t.target_pct,
  t.target_pct - COALESCE(c.weight_pct, 0) AS gap_pct,
  (t.target_pct - COALESCE(c.weight_pct, 0)) / 100 * a.equity_usd AS gap_usd,
  CASE
    WHEN COALESCE(c.weight_pct, 0) < t.band_low_pct THEN 'under'
    WHEN COALESCE(c.weight_pct, 0) > t.band_high_pct THEN 'over'
    ELSE 'in_band'
  END AS band_status
FROM ...
```

Optional `/drift` endpoint that returns just the out-of-band rows — useful for a quick manual check.

---

## 7. Wire the daily report job

`src/investor/jobs/daily_report.py`:

```python
def run_daily_report(app):
    settings = app.state.settings
    adapter = app.state.broker
    emailer = app.state.emailer
    with session_scope() as s:
        take_snapshot(adapter, s)        # write today's row first
        report = compose_daily_report(s)
    html = render_template("daily_report.html.j2", report=report)
    text = render_template("daily_report.txt.j2", report=report)
    emailer.send(
        to=settings.email_to,
        subject=f"Portfolio — {report.date:%Y-%m-%d} (equity ${report.account.equity_usd:,.0f})",
        html=html, text=text,
    )
    log.info("daily report sent to %s", settings.email_to)
```

Note the order: snapshot **first**, then read it back through the gap query. Don't compose from in-memory broker data — always go through the DB so the report matches what you can later audit.

Failure handling: if the snapshot succeeds but email fails, log the error and re-raise. APScheduler's default behavior records the failure; combine with `misfire_grace_time` so a retry happens automatically on next-fire if the SMTP server was briefly down. Do **not** silently swallow email errors — silent email failures are how teams discover months later that nobody got reports.

---

## 8. Bar data backfill

`scripts/backfill_bars.py`:

```python
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta, UTC
from pathlib import Path

def backfill(tickers: list[str], years: int = 2):
    client = StockHistoricalDataClient(API_KEY, SECRET)
    end = datetime.now(UTC) - timedelta(minutes=15)   # respect SIP delay on free tier
    start = end - timedelta(days=365 * years)
    out_dir = Path("data/bars"); out_dir.mkdir(parents=True, exist_ok=True)
    request = StockBarsRequest(
        symbol_or_symbols=tickers, timeframe=TimeFrame.Day,
        start=start, end=end, feed="iex",   # free feed
    )
    bars_df = client.get_stock_bars(request).df.reset_index()
    for ticker, group in bars_df.groupby("symbol"):
        group.drop(columns=["symbol"]).to_parquet(out_dir / f"{ticker}.parquet")
        print(f"  {ticker}: {len(group)} rows → {out_dir / f'{ticker}.parquet'}")
```

Run once: `uv run python scripts/backfill_bars.py`. Result: `data/bars/VOO.parquet`, `data/bars/QQQ.parquet`, etc. Roughly 500 rows × 6 tickers × 80 bytes ≈ 250 KB total. Negligible.

Schedule a thin update job (later in Phase 1 if time, otherwise Phase 2): every weekday after close, append the latest bar to each Parquet. Pattern: read existing Parquet, append today's row, write back.

---

## 9. `price_bar` view

Two valid approaches with DuckDB:

**Option A — Parquet-only with a SQL view.** Recommended for analytical workloads.

```sql
CREATE OR REPLACE VIEW price_bar AS
SELECT * FROM read_parquet('data/bars/*.parquet', filename=true)
  -- filename column gives you the ticker; cast/extract as needed
```

Or the explicit form (auto-generate this from the watchlist if you don't want hardcoded tickers):

```sql
CREATE OR REPLACE VIEW price_bar AS
SELECT 'VOO' AS ticker, * FROM 'data/bars/VOO.parquet' UNION ALL
SELECT 'QQQ' AS ticker, * FROM 'data/bars/QQQ.parquet' UNION ALL
...
```

**Option B — ingest into a DuckDB table.** Simpler queries, but you lose the ability to swap Parquet files in and have the DB pick them up.

Pick A. Phase 2's indicator queries will read this view directly:

```sql
SELECT ticker, date,
       AVG(close) OVER (PARTITION BY ticker ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sma_200
FROM price_bar
WHERE date >= today() - 365
```

That's a one-liner instead of a pandas pipeline.

---

## 10. Integration test against Alpaca paper

One test that exercises the full chain: pull positions, write snapshot, compute gap, render templates, send email via `FakeEmailer`. Tag with `@pytest.mark.integration` so unit tests stay fast and CI can opt in:

```python
# tests/test_integration_alpaca.py
@pytest.mark.integration
def test_alpaca_paper_full_chain(tmp_db):
    if not os.getenv("ALPACA_API_KEY"):
        pytest.skip("no API keys present")
    settings = Settings()
    adapter = make_adapter(settings)
    fake = FakeEmailer()
    with session_scope(tmp_db) as s:
        take_snapshot(adapter, s)
        report = compose_daily_report(s)
    html = render_template("daily_report.html.j2", report=report)
    fake.send(to="x@y", subject="t", html=html, text="t")
    assert len(fake.sent) == 1
    assert "VOO" in fake.sent[0]["html"]
```

Add to CI as an optional job that runs only on push to main, so PRs don't burn API quota.

---

## 11. Smoke-test checklist (Phase 1 done when all green)

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run alembic upgrade head` (after ADR-0002) | DB schema in sync with models, no errors |
| 2 | `uv run python scripts/load_targets.py` × 3 with no YAML change | Exactly one set of open `target_allocation` rows; no duplicates |
| 3 | `uv run pytest tests/test_load_targets.py` | All cases pass (idempotency + versioning) |
| 4 | `uv run python scripts/backfill_bars.py` | One Parquet file per watchlist ticker in `data/bars/` |
| 5 | `duckdb data/investor.duckdb -c "SELECT ticker, COUNT(*) FROM price_bar GROUP BY ticker"` | Row count > 400 per ticker |
| 6 | `uv run python -m investor.jobs.daily_report` (manual) | Email lands in `EMAIL_TO` inbox; HTML + text both readable |
| 7 | Email visual check on Gmail web | Allocation table aligned, drift alerts highlighted |
| 8 | Email visual check on iPhone Mail | Same — no broken layout |
| 9 | `uv run pytest -m "not integration"` | All unit tests pass in < 5 s |
| 10 | `uv run pytest -m integration` | Integration test passes against Alpaca paper |
| 11 | `docker compose up -d`, wait through one 16:15 ET cron fire | New positions snapshot row + email received |
| 12 | Edit `targets.yaml`, hit `POST /admin/reload-targets`, hit `/gap` | Old targets show `effective_to`; new targets show `effective_to IS NULL`; gap reflects new targets |
| 13 | Restart container; `curl /health` | `last_sync_ts` unchanged from before restart (no spurious re-sync) |
| 14 | Five consecutive trading-day emails received | No misfires; if you were offline at 16:15, the misfire grace ran it on next start |

Tag and push:

```bash
git add -A
git commit -m "phase 1: daily portfolio email + bar backfill"
git tag v0.1.0-phase-1
git push --tags
```

---

## 12. Common Phase 1 pitfalls

1. **Gmail 535 auth error.** Almost always: you used your account password, not an App Password; or the App Password lost a space when copy-pasted. Regenerate, paste carefully.
2. **Email lands in spam.** Set a friendly `From:` and a plain-text part. If still spammed, send to yourself once with the subject "test" and click "Not spam" once — Gmail learns fast.
3. **APScheduler missed fire when laptop was asleep.** That's why `misfire_grace_time` is set. Verify with `journalctl` / Docker logs that a misfired job actually ran on resume.
4. **Bar backfill timeouts on first run.** Alpaca rate-limits free-tier to 200 req/min. For your watchlist this is fine, but if you ever expand past ~30 tickers add a `time.sleep(0.3)` between requests.
5. **DuckDB lock during backfill.** Stop the FastAPI container before running `backfill_bars.py` or move the writes into a FastAPI endpoint that uses the same session pool.
6. **HTML rendering inconsistencies.** Apple Mail honors `<style>`, Gmail web partly does, Outlook desktop does its own thing. Always test in two clients before shipping.
7. **`misfire_grace_time` over-fires.** If you set it to 24 h and your Mac was off for two days, you'll get one (not three) catch-up emails — that's correct; APScheduler de-dupes by job ID. If you want a backfill-on-resume pattern, you have to write it explicitly.
8. **CronTrigger DST confusion.** `timezone="America/New_York"` handles DST automatically; if you used `"EST"` instead you'll be off by an hour for half the year. Use the IANA zone name, not the abbreviation.

---

## 13. ADRs to write in Phase 1

- `docs/adr/0002-schema-migrations.md` — Alembic vs inline; landing on Alembic with batch_alter_table.
- `docs/adr/0003-bar-storage.md` — Parquet-only with SQL view (Option A above).
- `docs/adr/0004-email-failure-policy.md` — re-raise vs swallow; what counts as a missed report; how often we'll tolerate failures before escalating.

Three short ADRs; together less than an hour of writing. Future-you and any future Claude session will thank present-you.

---

*When all 14 smoke-test rows are green, you've received 5 consecutive scheduled trading-day emails, and ADRs 0002–0004 are committed, Phase 1 is done. Tag `v0.1.0-phase-1` and start Phase 2.*
