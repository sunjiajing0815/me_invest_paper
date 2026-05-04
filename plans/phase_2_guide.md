# Phase 2 — Technical Levels & Weekly Order Suggestions: Step-by-Step Guide

**Goal:** End with a Sunday evening email titled "Orders for the week of Mon DD" containing ranked, concrete buy/trim suggestions per ticker — anchored on technical levels (nearest support/resistance, MAs) and the gap engine. Suggestions are first-class rows in `order_suggestion` with `status = "pending"`, ready for Phase 4's accept/reject workflow. The system continues to never place orders.

**Out of scope for Phase 2:** weekly review with accept/reject UI (Phase 4), news + LLM (Phase 3), web UI (Phase 5). The daily portfolio email continues unchanged from Phase 1, with the addition of a compact "Levels at a glance" section.

**Time budget:** 5–7 evenings (15–21 focused hours). The technical-analysis math is the main effort; only one carryover (`/admin/*` auth) and one wiring task (`update_bars.py` into the scheduler) remain from Phase 1 — together about 1 hour.

**Definition of done:** all 16 smoke-test rows pass *and* you've received a single Sunday "Orders for the week" email whose suggestions read as sensible to you when you look at them — not "what?" One credible weekly email is the bar; multi-week soak is Phase 4 territory.

---

## Architecture context — what's new in Phase 2

Phase 1 established the OLTP/Analytics/Bars three-tier split. Phase 2 doesn't change that — it fills in the analytics tier with real work and adds two new OLTP tables (`sr_level`, `order_suggestion`).

```
                                     ┌──────────────────┐
                                     │ services/        │
                                     │   indicators.py  │  reads price_bar (DuckDB on Parquet)
                                     │   levels.py      │  reads price_bar; writes sr_level (SQLite)
                                     │   suggest.py     │  reads gap + sr_level; writes order_suggestion
                                     └──────────────────┘
                                              ▲
                                              │ called by
                                              │
  ┌──────────────────────────────┐    ┌──────────────────────┐
  │ jobs/daily_report.py         │    │ jobs/weekly_orders.py│
  │   update_bars + take_snap    │    │   update_bars +      │
  │   + compose + email          │    │   indicators+levels+ │
  │   (Mon–Fri 16:15 ET)         │    │   suggest + email    │
  └──────────────────────────────┘    │   (Sun 18:00 ET)     │
                                      └──────────────────────┘
```

The two new OLTP tables both go in SQLite via Alembic. The indicator / level computation is pure DuckDB-on-Parquet — no SQLAlchemy involvement until results are persisted.

---

## 0. Pre-flight checklist

- [ ] Phase 1 v0.1.0 tag is either pushed or close enough (5-day email streak running)
- [ ] All Phase 1 smoke tests still pass: `curl /health`, `curl /gap`, `curl /drift` all return non-empty
- [ ] `data/bars/*.parquet` files exist and `uv run python scripts/show_bars.py` shows ≥ 400 bars per ticker
- [ ] `pandas-ta` available: `uv add pandas-ta` (used for RSI/MACD which are awkward in pure SQL)
- [ ] Read `product_plan.md` §7 open questions 1, 2, 4 — they all gate Phase 2 design choices, and you should pick before coding

---

## 1. Resolve Phase 1 carryovers first (~1 hour)

The 2026-05-05 pre-tag cleanup pass closed six of the seven Phase 1 carryovers (ADRs 0002 + 0003, cash-buffer evaluation, Bug 2 regression test, `AccountSnapshot` generalization sweep, Python 3.12 host pin). Only one remains, plus one wiring task that's properly Phase 2 work.

### 1a. Admin token auth (carryover from Phase 1)

Three lines of dependency injection prevents Phase 4's accept/reject endpoints from quietly inheriting the no-auth pattern.

In `.env` and `.env.example`:
```bash
ADMIN_TOKEN=<generate-with: openssl rand -hex 32>
```

In `src/investor/main.py`:
```python
from fastapi import Depends, Header, HTTPException

def admin_auth(
    x_admin_token: str = Header(default=""),
    settings = Depends(get_settings),
):
    if not settings.admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")
```

Apply to every `/admin/*` route:
```python
@app.post("/admin/run-sync", dependencies=[Depends(admin_auth)])
def run_sync(): ...
```

Update `tests/test_integration_alpaca.py` to set the header. Manual testing now uses:
```bash
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/admin/run-sync
```

### 1b. Wire `update_bars.py` into the daily job

This is Phase 2 main work but it's small enough to do alongside the auth fix. In `src/investor/jobs/daily_report.py`, add a step before `take_snapshot`:

```python
def run_daily_report(settings, adapter, emailer):
    try:
        update_bars(settings.watchlist)   # tolerates failure
    except Exception as e:
        log.warning("update_bars failed; continuing with stale bars: %s", e)
    with session_scope() as s:
        take_snapshot(adapter, s)
        report = compose_daily_report(s)
    ...
```

Why tolerate failure: stale bars are better than no email. The next day's run retries. If `update_bars` is failing reliably, you'll notice by reading the email and seeing yesterday's prices.

---

## 2. Indicators service

`src/investor/services/indicators.py` — DuckDB for the SMAs (clean window-function SQL), pandas-ta for RSI and MACD (recursive math, painful to do in SQL).

```python
from dataclasses import dataclass
from datetime import date
import duckdb
import pandas as pd
import pandas_ta as ta
from investor.services.analytics import duckdb_conn

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
    pct_from_sma_50: float | None
    pct_from_sma_200: float | None


def compute_indicators(tickers: list[str]) -> list[IndicatorRow]:
    """Returns one row per ticker, latest available date.

    SMAs computed in DuckDB via window functions (vectorized).
    RSI/MACD computed via pandas-ta on the per-ticker series.
    """
    rows: list[IndicatorRow] = []
    with duckdb_conn() as con:
        # Bulk SMA pass — one query, all tickers
        sma_sql = """
            WITH augmented AS (
                SELECT
                    ticker, date, close,
                    AVG(close) OVER (PARTITION BY ticker ORDER BY date
                                     ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)  AS sma_20,
                    AVG(close) OVER (PARTITION BY ticker ORDER BY date
                                     ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)  AS sma_50,
                    AVG(close) OVER (PARTITION BY ticker ORDER BY date
                                     ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS sma_200,
                    ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC)   AS rn
                FROM price_bar
                WHERE ticker = ANY(?)
            )
            SELECT ticker, date, close, sma_20, sma_50, sma_200
            FROM augmented WHERE rn = 1
        """
        sma_rows = con.execute(sma_sql, [tickers]).fetchall()

        # RSI/MACD/EMA per ticker (recursive — needs full series)
        for ticker, as_of, close, sma_20, sma_50, sma_200 in sma_rows:
            df = con.execute(
                "SELECT date, close FROM price_bar WHERE ticker = ? ORDER BY date",
                [ticker],
            ).df()
            ema_21 = float(ta.ema(df["close"], length=21).iloc[-1])
            rsi_14 = float(ta.rsi(df["close"], length=14).iloc[-1])
            macd_df = ta.macd(df["close"])
            macd, signal = float(macd_df["MACD_12_26_9"].iloc[-1]), float(macd_df["MACDs_12_26_9"].iloc[-1])

            rows.append(IndicatorRow(
                ticker=ticker, as_of=as_of, close=close,
                sma_20=sma_20, sma_50=sma_50, sma_200=sma_200,
                ema_21=ema_21, rsi_14=rsi_14,
                macd=macd, macd_signal=signal,
                pct_from_sma_50  = (close / sma_50  - 1) * 100 if sma_50  else None,
                pct_from_sma_200 = (close / sma_200 - 1) * 100 if sma_200 else None,
            ))
    return rows
```

Things to internalize:

- **One SQL pass for SMAs across all tickers** is much faster than per-ticker loops. DuckDB's window functions run vectorized.
- **RSI / MACD / EMA need the full series**, not just the latest row, because they're recursive. Pulling the per-ticker `close` series into pandas and using pandas-ta is the cleanest path. Don't try to do RSI in pure SQL.
- **No persistence here.** This is a read-only service; nothing goes back to SQLite.

Add `/indicators` endpoint to `main.py`:
```python
@app.get("/indicators")
def indicators(settings = Depends(get_settings)):
    return compute_indicators(settings.watchlist)
```

---

## 3. Support/resistance levels

`src/investor/services/levels.py`. Three layered methods, in increasing subjectivity:

### 3a. Classical pivot points (formulaic, boring, work)

Computed on the prior period's bar:

```
Pivot       P  = (H + L + C) / 3
Support 1   S1 = 2P − H
Support 2   S2 = P − (H − L)
Resistance1 R1 = 2P − L
Resistance2 R2 = P + (H − L)
```

Where `H`, `L`, `C` are the previous period's high, low, close. Compute daily, weekly, and monthly variants. The daily pivots are noise for long-term strategies; weekly and monthly are useful.

### 3b. Moving-average bands (dynamic S/R)

The 20/50/200 SMA and 21 EMA from §2. Below current price → support; above → resistance. Each MA is one level; record method as `"sma_50_support"` etc.

### 3c. Swing highs/lows (fractal method)

A bar is a **swing high** if its `high` is greater than the highs of the N preceding and N following bars. **Swing low** mirrors. N = 5 is a reasonable default; tunable.

```python
def swing_levels(df: pd.DataFrame, n: int = 5) -> list[tuple[date, float, str]]:
    """Returns list of (date, price, kind) for swing highs/lows in the series."""
    out = []
    for i in range(n, len(df) - n):
        window = df.iloc[i - n : i + n + 1]
        h, l = df.iloc[i]["high"], df.iloc[i]["low"]
        if h == window["high"].max():
            out.append((df.iloc[i]["date"], h, "swing_high"))
        if l == window["low"].min():
            out.append((df.iloc[i]["date"], l, "swing_low"))
    return out
```

**Critical note:** the most recent N bars can never confirm a swing — they're "pending." Drop them from the result, or label them as unconfirmed. The Phase 1 gotcha #4 lives here.

### 3d. Persist into `sr_level`

Add the model in `models.py` and an Alembic revision:

```python
class SRLevel(Base):
    __tablename__ = "sr_level"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str]
    type: Mapped[str]                       # "support" | "resistance"
    price: Mapped[float]
    method: Mapped[str]                     # "pivot_weekly_S1", "sma_50", "swing_low_5bar", ...
    as_of: Mapped[date]                     # date the level was computed for
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    __table_args__ = (UniqueConstraint("ticker", "method", "as_of", name="uq_sr_per_method_per_day"),)
```

Persist on every weekly run. Cheap (small table) and useful for Phase 4 retrospectives ("which levels actually held?").

### 3e. The `nearby_levels` dataclass

For each ticker, return the 3 nearest supports below current price and 3 nearest resistances above:

```python
@dataclass(frozen=True)
class NearbyLevels:
    ticker: str
    current_price: float
    supports: list[SRLevelRow]              # 3 nearest below, sorted ascending distance
    resistances: list[SRLevelRow]           # 3 nearest above, sorted ascending distance
```

`SRLevelRow` is the dataclass equivalent of the ORM model — same conversion-at-boundary pattern.

---

## 4. Order suggestion engine

`src/investor/services/suggest.py`. Pure function: gap rows + nearby levels → order suggestions.

```python
@dataclass(frozen=True)
class OrderSuggestionRow:
    ticker: str
    side: Literal["buy", "sell"]
    qty: float
    limit_price: float
    reason: str                              # human-readable
    expires_at: datetime

def generate_suggestions(
    *, gap_rows: list[GapRow], nearby_levels: dict[str, NearbyLevels],
    account: AccountSnapshot, sizing_rule: SizingRule,
    cash_floor: float = 100, max_distance_pct: float = 8.0,
) -> list[OrderSuggestionRow]:
    out, cash_remaining = [], account.cash_usd

    for g in gap_rows:
        if g.band_status == "in_band":
            continue

        if g.gap_pct > 0:                                  # under-weight → buy
            levels = nearby_levels[g.ticker].supports
            if not levels:                                  # no support nearby; skip
                continue
            level = levels[0]                               # nearest
            if abs((level.price / nearby_levels[g.ticker].current_price - 1) * 100) > max_distance_pct:
                continue                                    # support too far away
            dollars = sizing_rule.dollars_for(g.gap_usd)    # e.g., half the gap
            qty = round_qty(dollars / level.price)
            if qty < 1 or qty * level.price > cash_remaining - cash_floor:
                continue
            cash_remaining -= qty * level.price
            out.append(OrderSuggestionRow(
                ticker=g.ticker, side="buy", qty=qty,
                limit_price=level.price,
                reason=f"underweight {g.gap_pct:+.1f}% — buy at {level.method} ${level.price:,.2f}, closes {dollars/g.gap_usd*100:.0f}% of gap",
                expires_at=next_friday_eod(),
            ))

        elif g.band_status == "over":                       # over-band → trim at resistance
            levels = nearby_levels[g.ticker].resistances
            if not levels: continue
            level = levels[0]
            ...                                             # mirror logic for sell
    return out
```

Guards baked into the function:
- **Cash sufficiency** with a configurable floor (don't drain to zero).
- **Distance limit** — if nearest support is > 8 % away, skip rather than wait for an unrealistic level. Configurable.
- **Min share count** — drop sub-1-share suggestions unless fractional is enabled.
- **Wash-sale stub** — placeholder argument, real implementation needs `order_execution` history (Phase 4).

The `SizingRule` is its own object so you can swap "half the gap" for "fixed weekly dollars" without touching the engine. Document the default in ADR-0007.

### 4a. `order_suggestion` table

```python
class OrderSuggestion(Base):
    __tablename__ = "order_suggestion"
    id: Mapped[int] = mapped_column(primary_key=True)
    week_of: Mapped[date]                                   # Monday of upcoming week
    ticker: Mapped[str]
    side: Mapped[str]
    qty: Mapped[float]
    limit_price: Mapped[float]
    reason: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")  # pending|accepted|rejected|expired
    target_allocation_id: Mapped[int]                       # which targets were live
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime]                            # default: Friday 21:00 UTC of week_of
    __table_args__ = (UniqueConstraint("week_of", "ticker", "side", name="uq_one_per_ticker_per_week"),)
```

Run `uv run alembic revision --autogenerate -m "phase2 order_suggestion sr_level"`. Inspect — Alembic with batch mode for SQLite usually generates correct DDL but eyeball it.

The `UniqueConstraint` is what protects against duplicate inserts when the weekly job re-runs Sunday evening due to misfire grace.

### 4b. Persist with conflict handling

```python
def persist_suggestions(session, rows: list[OrderSuggestionRow], targets_id: int, week_of: date):
    for r in rows:
        existing = session.scalar(
            select(OrderSuggestion).where(
                OrderSuggestion.week_of == week_of,
                OrderSuggestion.ticker == r.ticker,
                OrderSuggestion.side == r.side,
            )
        )
        if existing:                                        # update existing pending row
            if existing.status == "pending":
                existing.qty = r.qty; existing.limit_price = r.limit_price
                existing.reason = r.reason
            continue                                        # don't overwrite accepted/rejected
        session.add(OrderSuggestion(...))
    session.commit()
```

Never overwrite a non-`pending` row — that destroys audit trail. Only refresh `pending` rows when the engine re-runs.

---

## 5. Weekly orders job

`src/investor/jobs/weekly_orders.py`:

```python
def run_weekly_orders(settings, adapter, emailer):
    update_bars(settings.watchlist)

    indicators = compute_indicators(settings.watchlist)

    with session_scope() as s:
        # latest snapshot — re-use Phase 1 take_snapshot
        take_snapshot(adapter, s)
        gap_rows = compute_gap(s)
        account = get_latest_account_snapshot(s)
        targets_id = get_active_targets_id(s)

        sr_rows = compute_levels(settings.watchlist, indicators)
        persist_levels(s, sr_rows)

        nearby = build_nearby_levels(settings.watchlist, sr_rows, indicators)

        suggestions = generate_suggestions(
            gap_rows=gap_rows, nearby_levels=nearby,
            account=account, sizing_rule=HALF_THE_GAP,
        )
        persist_suggestions(s, suggestions, targets_id, week_of=next_monday())

    # email outside the session
    html = render_template("weekly_orders.html.j2",
                           week_of=next_monday(), account=account,
                           suggestions=suggestions, indicators=indicators, nearby=nearby)
    text = render_template("weekly_orders.txt.j2", ...)
    emailer.send(
        to=settings.email_to,
        subject=f"Orders for the week of {next_monday():%b %d}",
        html=html, text=text,
    )
```

Cron schedule:
```python
sched.add_job(
    run_weekly_orders,
    trigger=CronTrigger(day_of_week="sun", hour=18, minute=0,
                       timezone="America/New_York"),
    id="weekly_orders",
    misfire_grace_time=60 * 60 * 6,                         # 6 h grace
)
```

`POST /admin/run-weekly-orders` for manual trigger.

---

## 6. Email templates — weekly orders

`templates/weekly_orders.html.j2` and `weekly_orders.txt.j2`. Same constraints as the daily email (no external CSS, plain-text version mandatory).

Sections:

1. **Header** — "Orders for the week of MM-DD", account equity, deployable cash (`cash − cash_floor`).
2. **Suggestions table** — one row per pending suggestion, sorted by gap magnitude:
   ```
   Ticker | Side | Qty | Limit  | Current | Distance | Reason
   AAPL   | BUY  |   8 | $182.4 | $185.20 | +1.5%    | underweight 8.2% — buy at sma_50 support, closes 50% of gap
   QQQ    | TRIM |   3 | $452.0 | $448.10 | +0.9%    | overweight 3.4% — trim at pivot_weekly_R1, closes 50% of gap
   ```
3. **Top-line** — total $ to deploy, # buys, # sells.
4. **Reminder** — "No orders are placed automatically. Log into Alpaca to act."

Develop by rendering to a static `.html` file first, browser preview, **then** wire SMTP.

---

## 7. Indicators in the daily email

Extend `DailyReport` (in `services/daily_report.py`):

```python
@dataclass(frozen=True)
class DailyReport:
    date: date
    account: AccountSnapshot
    positions: list[PositionRow]
    gap_rows: list[GapRow]
    drift_alerts: list[GapRow]
    indicators: list[IndicatorRow]                          # NEW
    nearby_levels: dict[str, NearbyLevels]                  # NEW
```

Add to `templates/daily_report.html.j2` a compact "Levels at a glance" section after the allocation table:

```
Ticker | Last  | %Δ50SMA | %Δ200SMA | RSI14 | Nearest Support | Nearest Resistance
AAPL   | 185.2 | +1.8%   | +12.4%   |  58   | $182.4 (sma_50) | $189.0 (pivot_R1)
```

Keep this terse — the daily email shouldn't become a wall of indicators. Two screen-widths max.

---

## 8. Smoke-test checklist (Phase 2 done when all green)

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run alembic upgrade head` | New migration applied; `order_suggestion`, `sr_level` tables exist |
| 2 | `uv run pytest -m "not integration"` | All Phase 2 tests pass in <10 s; total now ≥ 40 |
| 3 | Phase 1 carryover regression test (compose_daily_report session-safety) | Passes |
| 4 | `curl -H "X-Admin-Token: $ADMIN_TOKEN" -X POST localhost:8000/admin/run-sync` | 200 |
| 5 | `curl -X POST localhost:8000/admin/run-sync` (no token) | 401 |
| 6 | `curl localhost:8000/indicators \| jq` | One row per watchlist ticker; SMAs/EMAs/RSI all populated |
| 7 | `curl -X POST -H "X-Admin-Token: …" localhost:8000/admin/run-weekly-orders` | 200; `order_suggestion` rows inserted |
| 8 | `sqlite3 data/investor.db "SELECT * FROM order_suggestion ORDER BY id DESC LIMIT 10"` | Rows with `status='pending'`, plausible limit prices, populated reasons |
| 9 | `sqlite3 data/investor.db "SELECT * FROM sr_level WHERE as_of = (SELECT MAX(as_of) FROM sr_level)"` | Three or more methods per ticker |
| 10 | Email landed: "Orders for the week of …" | Suggestions table renders; reasons human-readable |
| 11 | Manual eyeball: each suggestion makes sense | "Buy AAPL at $182.4 because 50-SMA support and we're underweight 8%" — does this match what you'd do? |
| 12 | Daily email picks up new "Levels at a glance" section | Renders cleanly on Gmail web + iPhone Mail |
| 13 | Re-running weekly job mid-week | No duplicate `order_suggestion` rows (UniqueConstraint enforces) |
| 14 | Cash-buffer invariant: $100k equity / $5k cash with targets summing to 95 | All `gap_pct == 0.0` (asserts the invariant comment in `gap_allocation.sql` still holds) |
| 15 | `update_bars` runs as part of daily job; `data/bars/*.parquet` modified time ≤ 24 h | No "stale bars" warning in logs |
| 16 | One Sunday email observed (definition of done) | You read it, suggestions feel sensible, no "what?" |

Tag and push:

```bash
git add -A
git commit -m "phase 2: technical levels + weekly order suggestions"
git tag v0.2.0-phase-2
git push --tags
```

---

## 9. Common Phase 2 pitfalls

1. **Window function partial windows.** `ROWS BETWEEN 199 PRECEDING AND CURRENT ROW` over a partition with < 200 rows produces a partial-window average — not nothing, not null. Filter `WHERE date >= start_of_series + 200_days` in the consuming code, or accept that early-data SMAs are not real SMAs.
2. **RSI initial values unreliable.** Needs ~30 bars before stable. Don't use the first month of data for any RSI-driven decision.
3. **Pivot points use the prior period's bar, not the current.** Daily pivots use yesterday's H/L/C. Weekly pivots use last week's. Reading the formula carelessly produces garbage levels.
4. **Swing-high lookback ambiguity.** A swing-high requires N bars on **each side**. The most recent N bars can never confirm a swing — they're pending. Drop them or label them; do not include unconfirmed pivots in `nearby_levels`.
5. **DST and Sunday cron.** APScheduler with `timezone="America/New_York"` handles this; don't write any `+5h` math anywhere.
6. **Limit price too far from current.** "Buy at 50-SMA support" is meaningless if the SMA is 20 % below current. The `max_distance_pct` guard in §4 catches this. Tune to your taste.
7. **Wash-sale stub vs. real check.** Phase 2's wash-sale guard is a placeholder; the real check needs `order_execution` data which Phase 4 builds. Don't ship live trading on Phase 2 confidence.
8. **OrderSuggestion duplicates.** Without the UniqueConstraint, re-running the weekly job mid-week silently duplicates rows. Catch this in test 13.
9. **`pandas-ta` returns NaN for warmup periods.** Wrap in `float() or None`. Don't insert NaN into SQLite — it gets weird.
10. **Emailing the suggestions before persisting them** is a real footgun: if the email succeeds and the DB write fails, the user acts on suggestions that have no audit trail. Order-of-operations in §5: persist first, then email. The email is the side effect; the database row is the source of truth.
11. **Persisting `sr_level` rows on every run** can balloon if you're not careful — N tickers × M methods × 7 trading days = a lot. The `UniqueConstraint(ticker, method, as_of)` keeps it bounded. Don't drop it.
12. **`positions_snapshot` retention.** ~1500 rows/year stays manageable but Phase 2 is a good time to add a pruning job: keep daily for 90 days, then weekly for 1 year, then monthly. Defer if low priority.

---

## 10. ADRs to write in Phase 2

ADRs 0002 and 0003 (the retroactive pair) and the cash-buffer decision were all closed in the 2026-05-05 pre-tag cleanup. Only the new Phase 2 ADRs remain:

- `docs/adr/0006-sr-methodology.md` — what S/R methods we use, why pivots before swing, why MAs as dynamic S/R. Brief.
- `docs/adr/0007-position-sizing.md` — half-the-gap default, configurable via `targets.yaml`. Document the choice between half-gap, fixed-dollar, full-gap.

Two short ADRs; together less than 45 minutes.

---

## 11. Documentation drift to fix

- **CLAUDE.md** — update repo layout to include `services/indicators.py`, `services/levels.py`, `services/suggest.py`, `jobs/weekly_orders.py`. Add common command line for the weekly trigger. (The session-safety principle is already implicitly enforced via the `AccountSnapshot` + SQL-Row-tuple patterns the Phase 1 cleanup verified; if a Phase 2 service starts returning ORM objects directly, that's the moment to add convention #11.)
- **product_plan.md** — when Phase 2 ships, mark it complete and add a §6 phase-2 retro subsection (carrying any Phase 2 deviations into Phase 3).
- **ADRs index** — if you don't have one, add `docs/adr/README.md` with a one-line summary per ADR. Phase 4's accept/reject UI will be the first thing to actually traverse this list.

---

*When all 16 smoke-test rows are green, you've received and read one credible Sunday "Orders for the week" email, and ADRs 0006 and 0007 are committed, Phase 2 is done. Tag `v0.2.0-phase-2` and start Phase 3.*
