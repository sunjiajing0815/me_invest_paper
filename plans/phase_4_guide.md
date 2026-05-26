# Phase 4 — Reconciliation, Moomoo Adapter, Weekly Review, and Opt-in Auto-Trade: Step-by-Step Guide

**Goal:** Four deliverables that all touch `order_execution`. (1) **Reconciliation engine** — daily 16:45 ET cron pulls `account/activities` from the broker, matches against `order_suggestion` rows, populates `order_execution` as the audit trail. (2) **Moomoo `BrokerAdapter`** — implements `BrokerAdapter` against OpenD, runs read-only in parallel against Alpaca for 4+ weeks before any primary flip. (3) **Friday weekly review email** — realized PnL, reconciliation summary, drift state, material movers, next-Sunday preview. (4) **Opt-in auto-trade execution** — three-state mode (`OFF` default / `DRY_RUN` / `LIVE`), fires only on `accepted` suggestions, hard caps + wash-sale guard + idempotent client order IDs + read-back reconciliation + kill switch; staged broker-scope progression across Alpaca paper → Alpaca live → Moomoo.

**Out of scope for Phase 4:** Multi-tenant productization (Phase 5). Web UI (Phase 5). Target adjustment / rebalance reviews (Phase 5 — folded in from the original Phase 4.5). News-driven suggestion *generation* — news still flows through the Phase 3c suggestion-review graph only.

**Time budget:** ~3–3.5 weeks of code (15–20 evenings, 45–60 focused hours), plus 14–16 weeks of staged soak in calendar time. The four workstreams split roughly: reconciliation ~1 week (highest priority, populates the data the others depend on); Moomoo adapter ~1 week of code plus the 4-week parallel-run soak; weekly review email ~3 evenings; auto-trade framework ~1 week plus the 4 sequential soak windows.

**Definition of done — multi-tag** rather than single-tag. The phase is "code complete" when all four workstreams ship; "soak complete" stages with the auto-trade promotion progression.

| Tag | Milestone | Earliest calendar date |
|---|---|---|
| `v0.4.0-phase-4-code-complete` | All four workstreams shipped; auto-trade in `OFF`; one Friday review email received | ~3.5 weeks after start |
| `v0.4.1-paper-dry-run` | Auto-trade `DRY_RUN` on Alpaca paper, clean for 2 weeks | `v0.4.0` + ~2–3 weeks (needs reconciliation history first) |
| `v0.4.2-paper-live` | Auto-trade `LIVE` on Alpaca paper, clean for 4 weeks | `v0.4.1` + 4 weeks |
| `v0.4.3-alpaca-live` | Auto-trade `LIVE` on real Alpaca (small capital), clean for 4 weeks | `v0.4.2` + 4 weeks |
| `v0.4.4-moomoo-live` | Auto-trade `LIVE` on Moomoo after parallel-run + primary flip + 4 weeks | `v0.4.3` + Moomoo soak + flip + 4 weeks |

**Depends on:** Phase 3 (`v0.3.0-phase-3`) tagged. Order suggestions must be flowing through the LLM-reviewed pipeline before reconciliation produces meaningful data — reconciling against a mechanical Phase 2 suggestion engine would just verify the user agrees with mechanical picks, not the actual decision quality the project is trying to measure. Auto-trade in particular requires Phase 3c's accept/reject magic-link workflow (it fires only on `status='accepted'` rows).

---

## Architecture context — what's new in Phase 4

Phase 4 is the first phase where the system both **reads broker activity history** (reconciliation) and **writes orders to a broker** (auto-trade, gated). Up to now the daily sync pulled positions+account only; Phase 4 adds the transactional record + the optional write path. `order_execution` is the shared schema both touch, with the unique constraint `(broker_order_id, broker)` as the dedup boundary between two writers.

```
                                    ┌─────────────────────────────┐
                                    │ BrokerAdapter (Phase 0)     │
                                    │ + new methods:              │
                                    │   get_activities(since)     │
                                    │   submit_order(req) ← LIVE  │
                                    │   get_order(broker_id)      │
                                    │   cancel_order(broker_id)   │
                                    └──────────────┬──────────────┘
                                                   │
                ┌──────────────────────────────────┴───────────────────────────────┐
                ▼                                                                  ▼
    ┌──────────────────────┐                                          ┌──────────────────────┐
    │ AlpacaAdapter        │                                          │ MoomooAdapter (NEW)  │
    │ (existing Phase 0)   │                                          │ via OpenD on host    │
    │ + get_activities()   │                                          │ host.docker.internal │
    │ + submit_order()     │                                          │   :11111             │
    └──────────────────────┘                                          └──────────────────────┘
                ▲    ▲                                                            ▲
                │    │                                                            │
                │    │            ┌────────────────────────────┐                  │
                │    └────────────┤ services/auto_trade.py     │                  │
                │ Mon–Fri         │ • mode: OFF / DRY_RUN /    │                  │
                │ 9:35 ET         │   LIVE  (default OFF)      │                  │
                │ INSERT          │ • fires only on            │                  │
                │ new             │   status='accepted'        │                  │
                │ order_execution │ • client_order_id =        │                  │
                │ row             │   f"sug-{id}"              │                  │
                │ status=         │ • caps + wash-sale +       │                  │
                │ accepted_for_   │   read-back-60s            │                  │
                │ routing         │ • writes order_exec        │                  │
                │                 │   row immediately          │                  │
                │                 └────────────────────────────┘                  │
                │                                                                 │
                │              ┌─────────────────────┐                            │
                └──────────────┤ services/           │◀───────────────────────────┘
                  Mon–Fri      │   reconciliation.py │  Moomoo parallel-run
                  16:45 ET     │ • daily cron        │  (read-only first;
                  UPDATE       │ • match activities  │   flip primary later)
                  same row     │   → order_suggestion│
                  by broker_   │ • UPDATE existing   │
                  order_id     │   order_execution   │
                               │   rows (auto-trade  │
                               │   inserted them)    │
                               │ • INSERT new rows   │
                               │   for manual trades │
                               └──────────┬──────────┘
                                          │
                                          ▼
                               ┌─────────────────────┐
                               │ order_execution     │
                               │ (TWO writers, ONE   │
                               │  schema)            │
                               │                     │
                               │ dry_run=true  →     │
                               │   invisible to      │
                               │   reconciliation +  │
                               │   wash-sale         │
                               │ dry_run=false →     │
                               │   real audit row    │
                               └──────────┬──────────┘
                                          │
                ┌─────────────────────────┼────────────────────────┐
                ▼                         ▼                        ▼
       ┌────────────────┐  ┌─────────────────────┐  ┌──────────────────────┐
       │ wash-sale      │  │ weekly review       │  │ daily auto-trade     │
       │ guard          │  │ (Friday 17:00 ET)   │  │ summary email        │
       │ (auto-trade,   │  │ • realized PnL      │  │ (after 9:35 ET cron) │
       │  reads WHERE   │  │ • suggested-vs-     │  │ • placed / dry-run / │
       │  dry_run=false)│  │   filled table      │  │   rejected reasons   │
       └────────────────┘  │ • drift state       │  └──────────────────────┘
                           │ • next-week preview │
                           └─────────────────────┘
```

Seven behaviours to internalize:

1. **`get_activities` is new on `BrokerAdapter`** — the transactional history that lets the system answer "what trades actually happened on this account." Both adapters (Alpaca, Moomoo) implement it.
2. **`submit_order` is also new on `BrokerAdapter`, but only auto-trade in `LIVE` mode calls it.** Phase 0–3 used `submit_order_draft` only. Phase 4 introduces the real `submit_order` and `services/auto_trade.py` is the *single* file in the entire codebase outside `brokers/` permitted to invoke it. CLAUDE.md "Things to never do" enforces this; a `tests/test_no_unauthorized_submit_order.py` grep test enforces it in CI.
3. **Reconciliation is upsert-shaped, not insert-only.** When auto-trade has already written an `order_execution` row at 9:35 ET, reconciliation at 16:45 ET matches the same `broker_order_id` and *updates* the fill fields rather than creating a duplicate. The unique constraint on `(broker_order_id, broker)` is what makes this safe. Manual trades that auto-trade never knew about get inserted fresh.
4. **The Moomoo adapter ships read-only first.** Phase 4 introduces `MoomooAdapter` but `BROKER=alpaca_*` is the production setting throughout the auto-trade soak. The Moomoo adapter is exercised by a parallel-run job for 4+ weeks of position-comparison soak; flipping `BROKER=moomoo` is a separate manual decision after that.
5. **Auto-trade defaults to `OFF` everywhere — fresh installs, restarts, after any guard failure.** The mode lives in a `meta` table row, *not* in `Settings`/`.env` — an env var would make the default mutable by accident.
6. **DRY_RUN is invisible to reconciliation.** Rows with `dry_run=true` have `broker_order_id=NULL` and are filtered out at every matcher query (so reconciliation never tries to match against them) and every wash-sale-guard query (`WHERE dry_run = false`) so simulated losses can never block real buys.
7. **The weekly review email is a digest, not a generator.** Sunday's email (Phase 3c) generates next week's proposed orders. Friday's review (Phase 4) summarizes the week just ended and previews what Sunday is likely to suggest. Two cadences serving two mental modes: Monday-action vs. weekend-reflection.

---

## 0. Pre-flight checklist (~30 minutes)

- [ ] **Phase 3 tagged**: `v0.3.0-phase-3` pushed. All sub-phase pre-tag checklists green. If 3a, 3b, or 3c is still "code complete tag pending," don't start Phase 4 — fix the observed regression in the right sub-phase, then come back.
- [ ] **Phase 3c carryover #1 fixed:** the MU silent-failure in `score_all_tickers_parallel()`. Verify by inspecting `tests/test_weekly_suggestions.py` — should now include a test that an injected `JSONDecodeError` in scoring produces a logged traceback and a user-visible surface, not just `out[t] = []`. If not done, see §1a below.
- [ ] **Phase 3c carryover #2 noted:** the 8% → 15% `max_distance_pct` calibration is pending revisit with reconciliation data. Phase 4 generates that data; we'll come back to this in §6.
- [ ] **Moomoo account exists and OpenD is installed locally.** OpenD must run on the host (macOS/Windows), not in Docker. From `host.docker.internal:11111`, port 11111 is the default OpenD bind. Test connectivity from inside the running app container: `curl host.docker.internal:11111` returns *something* (even an error response indicates the host can be reached).
- [ ] **Moomoo paper account funded with $10k notional** (Moomoo OpenD's paper account is separate from Alpaca's). Match your Alpaca paper positions roughly so the parallel-run comparison is meaningful.
- [ ] **`futu-api` Python package installed:** `uv add 'futu-api>=9.3'` — Moomoo's Python client for OpenD. (Yes, `futu`, not `moomoo` — Moomoo and Futu share the OpenAPI stack.)
- [ ] **Phase 4 cron windows reserved:** 16:45 ET Mon–Fri (reconciliation), 17:00 ET Friday (weekly review). Both after the 16:30 movers job. Verify no existing job conflicts.
- [ ] All Phase 3 smoke tests still pass.

---

## 1. Resolve Phase 3c carryovers (~1 evening)

### 1a. Fix `score_all_tickers_parallel()` silent-failure (MU pattern)

The Phase 3c completion review's punch-list item #1. Three changes:

```python
# in services/weekly_suggestions.py or wherever score_all_tickers_parallel lives
from concurrent.futures import ThreadPoolExecutor, as_completed

@dataclass(frozen=True)
class ScoringFailure:
    ticker: str
    exc_type: str          # e.g. "JSONDecodeError", "TimeoutError"
    exc_message: str       # truncated to 200 chars

def score_all_tickers_parallel(
    tickers: list[str], llm: LLMClient, max_workers: int = 4,
) -> tuple[dict[str, list[ScoredLevel]], list[ScoringFailure]]:
    """Score all tickers in parallel. Returns (scored, failures).

    Failures are surfaced both via the log (with full traceback) and via
    the returned list so the caller can render them in the email. This
    is the SkippedRow pattern from Phase 3c §6.5a applied to scoring.
    """
    out: dict[str, list[ScoredLevel]] = {}
    failures: list[ScoringFailure] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(score_levels_for_ticker, llm=llm, ticker=t, ...): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                out[t] = fut.result()
            except (json.JSONDecodeError, pydantic.ValidationError) as e:
                log.warning("scoring schema-failure for %s", t, exc_info=True)
                failures.append(ScoringFailure(ticker=t, exc_type=type(e).__name__, exc_message=str(e)[:200]))
                out[t] = []
            except Exception as e:                  # noqa: BLE001 — intentional broad catch
                log.exception("scoring failed for %s", t)
                failures.append(ScoringFailure(ticker=t, exc_type=type(e).__name__, exc_message=str(e)[:200]))
                out[t] = []
    return out, failures
```

Three properties to verify in tests:

- Specific exception types (`JSONDecodeError`, `ValidationError`) are caught and logged at WARNING with `exc_info=True`.
- Generic exceptions are caught and logged at ERROR with `log.exception` (which auto-attaches traceback).
- The returned `failures` list is non-empty when any ticker failed, regardless of cause.

Update `jobs/weekly_suggestions.py` to receive the tuple and pass `failures` to the email template. Render in the "Levels at a glance" section with a small annotation: "MU: scoring failed (JSONDecodeError) — using nearest-distance fallback." User now sees which tickers paid the fallback cost.

`tests/test_weekly_suggestions.py` gains: `test_scoring_failure_surfaced` — inject a mock LLMClient that raises `JSONDecodeError` for one ticker, assert `failures` contains it, assert the log has the traceback.

### 1b. Note the 8% → 15% calibration for §6 review

No code change here; just acknowledgment that Phase 4's reconciliation data is the input you need to actually answer "was 15% the right number?" In §6 we'll check fill rates by anchor-distance bucket.

---

## 2. `order_execution` table + reconciliation (~1 week)

### 2a. Alembic migration

```python
def upgrade():
    op.create_table(
        "order_execution",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("suggestion_id", sa.Integer, sa.ForeignKey("order_suggestion.id"), nullable=True),
        # ^ nullable because untracked broker activity (manual trades) get rows too
        sa.Column("ticker", sa.String, nullable=False),
        sa.Column("side", sa.String, nullable=False),                     # buy | sell
        sa.Column("submitted_qty", sa.Float, nullable=True),              # what the suggestion called for
        sa.Column("filled_qty", sa.Float, nullable=False),                # what actually filled
        sa.Column("limit_price", sa.Float, nullable=True),                # from the suggestion
        sa.Column("filled_price", sa.Float, nullable=False),              # actual fill price
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("broker", sa.String, nullable=False),                   # alpaca | moomoo
        sa.Column("broker_order_id", sa.String, nullable=False),
        sa.Column("client_order_id", sa.String, nullable=True),           # populated by §5 auto-trade
        sa.Column("dry_run", sa.Boolean, nullable=False, server_default=sa.true()),
                                                                         # default true is the SAFER side
                                                                         # if any code path forgets to set it
        sa.Column("status", sa.String, nullable=False),                   # filled | partially_filled | rejected | expired
        sa.Column("realized_pnl_usd", sa.Float, nullable=True),           # only on sells
        sa.Column("match_method", sa.String, nullable=False),             # auto_matched | manual_review | untracked
        sa.Column("match_confidence", sa.Float, nullable=True),           # 0..1 from matching algorithm
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, default=sa.func.now()),
        sa.UniqueConstraint("broker_order_id", "broker", name="uq_broker_order_id"),
    )
    op.create_index("ix_order_execution_ticker_filled_at", "order_execution", ["ticker", "filled_at"])
```

The unique constraint on `(broker_order_id, broker)` is what makes reconciliation idempotent — re-running the matcher over already-matched activities produces no duplicate rows.

The `match_method` column distinguishes four categories:
- `auto_trade_placed`: the row was inserted by `services/auto_trade.py` at order-submission time; reconciliation updates fill fields when the activity arrives but leaves this label unchanged (it's the auto-trade audit marker)
- `auto_matched`: reconciliation matched a broker activity to a pending suggestion within tolerance window (used when auto-trade was off and the user placed the order manually)
- `manual_review`: ambiguous — multiple suggestions could match, or the match was outside tolerance but plausible (saved for human review via `/admin/reconcile` endpoint)
- `untracked`: no plausible suggestion match (you traded manually outside the system; row logged for completeness)

### 2b. `BrokerAdapter.get_activities()`

New method on the Protocol. Returns a list of `Activity` dataclasses.

```python
# brokers/base.py
@dataclass(frozen=True)
class Activity:
    broker_order_id: str
    client_order_id: str | None
    ticker: str
    side: Literal["buy", "sell"]
    filled_qty: float
    filled_price: float
    filled_at: datetime
    status: Literal["filled", "partially_filled", "rejected", "expired", "canceled"]

class BrokerAdapter(Protocol):
    # ... existing methods
    def get_activities(self, since: datetime, until: datetime | None = None) -> list[Activity]: ...
```

Alpaca implementation uses `TradingClient.get_account_activities` filtered to `FILL` and `PARTIAL_FILL` activity types. Moomoo implementation comes in §3.

### 2c. `services/reconciliation.py`

```python
@dataclass(frozen=True)
class MatchResult:
    suggestion_id: int | None
    activity: Activity
    method: Literal["auto_matched", "manual_review", "untracked"]
    confidence: float                  # 0..1

def reconcile_activities(
    *, session: Session, adapter: BrokerAdapter,
    since: datetime, ticker_tolerance_window_hours: int = 48,
    price_tolerance_pct: float = 0.5,
) -> list[MatchResult]:
    """Pull activities from broker, match to suggestions, return decisions.

    Matching rules (in priority order):
      1. If activity.client_order_id matches f"sug-{suggestion.id}" AND an existing
         order_execution row exists for that broker_order_id → this is an auto-trade
         fill; just update the existing row's fill fields. No new MatchResult inserted.
      2. If activity (ticker, side, qty) matches a pending suggestion within
         the time window and price tolerance → auto_matched, confidence 0.9
         (used when auto-trade is off and the user placed manually).
      3. If activity (ticker, side) matches multiple pending suggestions → manual_review
      4. If no plausible match → untracked
    """
    activities = adapter.get_activities(since=since)
    pending = session.scalars(
        select(OrderSuggestion).where(OrderSuggestion.status.in_(["accepted", "pending"]))
    ).all()

    results: list[MatchResult] = []
    for act in activities:
        # rule 1: client_order_id match (auto-trade case)
        if act.client_order_id and act.client_order_id.startswith("sug-"):
            sid = int(act.client_order_id.removeprefix("sug-"))
            results.append(MatchResult(suggestion_id=sid, activity=act,
                                       method="auto_matched", confidence=1.0))
            continue

        # rule 2+3: heuristic match
        candidates = [
            s for s in pending
            if s.ticker == act.ticker
            and s.side == act.side
            and abs((act.filled_at - s.created_at).total_seconds()) < ticker_tolerance_window_hours * 3600
            and abs(act.filled_price - s.limit_price) / s.limit_price <= price_tolerance_pct / 100
        ]
        if len(candidates) == 1:
            results.append(MatchResult(suggestion_id=candidates[0].id, activity=act,
                                       method="auto_matched", confidence=0.9))
        elif len(candidates) > 1:
            # pick best by qty match then price proximity; flag for manual review
            best = min(candidates, key=lambda s: abs(s.qty - act.filled_qty) + abs(s.limit_price - act.filled_price))
            results.append(MatchResult(suggestion_id=best.id, activity=act,
                                       method="manual_review", confidence=0.5))
        else:
            results.append(MatchResult(suggestion_id=None, activity=act,
                                       method="untracked", confidence=0.0))
    return results


def persist_reconciliation(session: Session, results: list[MatchResult], broker: str):
    """Upsert order_execution rows. Two paths:

    1. Existing row (auto-trade already inserted at 9:35 ET): UPDATE fill fields.
    2. No existing row (manual trade outside auto-trade): INSERT fresh.

    Idempotent via unique constraint on (broker_order_id, broker) + UPDATE-with-same-values
    being a no-op.
    """
    for r in results:
        existing = session.scalar(
            select(OrderExecution).where(
                OrderExecution.broker_order_id == r.activity.broker_order_id,
                OrderExecution.broker == broker,
                OrderExecution.dry_run.is_(False),     # never match against DRY_RUN rows
            )
        )
        if existing:
            # UPDATE path — auto-trade already inserted this row; populate fill data
            existing.filled_qty = r.activity.filled_qty
            existing.filled_price = r.activity.filled_price
            existing.filled_at = r.activity.filled_at
            existing.status = r.activity.status        # transitions from accepted_for_routing → filled
            if r.activity.side == "sell":
                existing.realized_pnl_usd = compute_realized_pnl(session, r.activity)
            # match_method / match_confidence stay as auto-trade set them
        else:
            # INSERT path — manual trade or activity for an order auto-trade didn't track
            session.add(OrderExecution(
                suggestion_id=r.suggestion_id,
                ticker=r.activity.ticker, side=r.activity.side,
                filled_qty=r.activity.filled_qty, filled_price=r.activity.filled_price,
                filled_at=r.activity.filled_at,
                broker=broker, broker_order_id=r.activity.broker_order_id,
                client_order_id=r.activity.client_order_id,
                dry_run=False,                         # reconciliation never inserts dry-run rows
                status=r.activity.status,
                match_method=r.method, match_confidence=r.confidence,
                realized_pnl_usd=(compute_realized_pnl(session, r.activity)
                                  if r.activity.side == "sell" else None),
            ))
        # Either path: if we matched a suggestion, flip its status to "filled"
        if r.suggestion_id and r.method == "auto_matched":
            sug = session.get(OrderSuggestion, r.suggestion_id)
            if sug and sug.status == "accepted":
                sug.status = "filled"
                sug.acted_at = r.activity.filled_at
    session.commit()
```

### 2d. Daily reconciliation cron

```python
sched.add_job(
    run_daily_reconciliation,
    trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=45,
                       timezone="America/New_York"),
    id="daily_reconciliation", misfire_grace_time=60 * 30,
)
```

16:45 ET runs after the 16:30 movers job. Pulls activities since the last successful reconciliation (or 7 days back on first run), matches, persists, logs counts.

### 2e. Realized PnL calculation

`realized_pnl_usd` on `order_execution` is only populated for sells. The calculation reads the matched buy-side history for the ticker and uses FIFO cost basis (the standard US tax-lot method) to compute realized gain or loss. This is the data Phase 4.6's wash-sale guard reads — a sell with `realized_pnl_usd < 0` in the last 30 days blocks a subsequent buy on the same ticker.

```python
def compute_realized_pnl(session: Session, sell: Activity) -> float:
    """FIFO cost basis. Reads prior buys from order_execution + manual baseline from positions_snapshot."""
    # implementation reads matching ticker's buy history, oldest-first, until qty satisfied
    # returns total realized PnL for this sell
```

Edge cases the implementation must handle:
- A sell with no prior buy history → realized_pnl unknown (NULL), log a warning
- Wash-sale guidance: if the same ticker has a loss-sell within 30 days and a re-buy after, the loss is *disallowed* and added to the cost basis of the new lot. Phase 4 records the data correctly; the disallow logic lives in Phase 4.6's guard.

### 2f. Admin endpoint for manual review

`POST /admin/reconcile/{execution_id}` with `{"suggestion_id": <int>}` lets you manually assign a `manual_review` row to a specific suggestion. Requires `X-Admin-Token`. Updates `match_method` to `manual_matched` and recomputes downstream fields. Simple, but valuable for the cases where the auto-matcher couldn't decide.

---

## 3. Moomoo adapter (`brokers/moomoo.py`) (~1 week + 2–4 week soak)

### 3a. OpenD setup on host

OpenD is the Moomoo-supplied gateway that bridges Moomoo's brokerage API to local Python clients. It runs on macOS or Windows on the host machine (not in Docker — there's no official Linux version).

Configure OpenD to:
- Bind to `0.0.0.0:11111` (not localhost) so Docker can reach it via `host.docker.internal:11111`.
- Use paper-trading mode for the parallel-run period.
- Auto-start on boot (macOS `launchd` plist or Windows Task Scheduler).

Document the OpenD startup in `docs/operations.md` so future-you doesn't lose half a day figuring out why the cron fired but Moomoo returned no data.

### 3b. `brokers/moomoo.py`

```python
from futu import (
    OpenSecTradeContext, OpenQuoteContext,
    TrdEnv, TrdSide, OrderType,
    SecurityFirm, RET_OK,
)
from investor.brokers.base import BrokerAdapter, Account, Position, Activity, OrderConfirmation

class MoomooAdapter:
    def __init__(self, host: str = "host.docker.internal", port: int = 11111,
                 paper: bool = True, security_firm: str = "FUTUSECURITIES"):
        self._host, self._port = host, port
        self._env = TrdEnv.SIMULATE if paper else TrdEnv.REAL
        self._security_firm = getattr(SecurityFirm, security_firm)
        # OpenD client connections — these are sync (futu-api is sync, not async)
        self._trade_ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US,
                                              host=host, port=port,
                                              security_firm=self._security_firm)
        self._quote_ctx = OpenQuoteContext(host=host, port=port)

    def get_account(self) -> Account:
        ret, data = self._trade_ctx.accinfo_query(trd_env=self._env, currency=Currency.USD)
        if ret != RET_OK:
            raise RuntimeError(f"moomoo accinfo_query failed: {data}")
        row = data.iloc[0]                       # futu-api returns DataFrames
        return Account(
            cash_usd=float(row["cash"]),
            equity_usd=float(row["total_assets"]),
            buying_power_usd=float(row["power"]),
            as_of=datetime.now(UTC),
        )

    def get_positions(self) -> list[Position]:
        ret, data = self._trade_ctx.position_list_query(trd_env=self._env)
        if ret != RET_OK:
            raise RuntimeError(f"moomoo position_list_query failed: {data}")
        return [
            Position(
                ticker=_strip_us_prefix(row["code"]),          # Moomoo prefixes with "US." — strip
                qty=float(row["qty"]),
                avg_cost=float(row["cost_price"]),
                market_value=float(row["market_val"]),
                as_of=datetime.now(UTC),
            )
            for _, row in data.iterrows()
        ]

    def get_activities(self, since: datetime, until: datetime | None = None) -> list[Activity]:
        ret, data = self._trade_ctx.deal_list_query(
            trd_env=self._env,
            start=since.strftime("%Y-%m-%d %H:%M:%S"),
            end=(until or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        if ret != RET_OK:
            raise RuntimeError(f"moomoo deal_list_query failed: {data}")
        return [
            Activity(
                broker_order_id=str(row["order_id"]),
                client_order_id=row.get("remark") or None,    # Moomoo stores client ID in remark field
                ticker=_strip_us_prefix(row["code"]),
                side="buy" if row["trd_side"] == TrdSide.BUY else "sell",
                filled_qty=float(row["qty"]),
                filled_price=float(row["price"]),
                filled_at=row["create_time"],
                status="filled",
            )
            for _, row in data.iterrows()
        ]

    def get_bars(self, ticker: str, start: datetime, end: datetime, timeframe: str = "1d") -> pd.DataFrame:
        # Moomoo bars come via the quote_ctx; identical schema to Alpaca after column rename.
        # Phase 4 uses Alpaca for bars regardless of BROKER setting — Moomoo's free-tier bars
        # have lower quota than Alpaca. Override in adapter only if Alpaca data is unavailable.
        raise NotImplementedError("Use AlpacaAdapter for bars; see ADR-0001 market-data separability")

    def submit_order_draft(self, draft: OrderDraft) -> OrderConfirmation:
        # Drafts mean "compute what we'd submit, return for user review" — no broker call.
        # Same shape as AlpacaAdapter.submit_order_draft.
        return OrderConfirmation(broker_order_id=f"draft-{uuid4()}", ...)

    def close(self):
        self._trade_ctx.close()
        self._quote_ctx.close()
```

Critical: Moomoo ticker symbols are prefixed with market code (`US.AAPL`, `HK.0700`). Strip the prefix at the adapter boundary so the rest of the app sees plain `AAPL`. ADR-0003 already documents the "domain IDs ≠ broker IDs" principle; this is its enforcement point for Moomoo.

### 3c. Parallel-run job

```python
# jobs/moomoo_parallel.py
def run_moomoo_parallel(settings, alpaca_adapter, emailer):
    """Read-only parallel poll. Compares Moomoo positions against Alpaca.

    Does NOT touch Moomoo's submit_order path. Pure read for the soak.
    """
    moomoo = MoomooAdapter(host=settings.opend_host, paper=True)
    try:
        alp_positions = {p.ticker: p for p in alpaca_adapter.get_positions()}
        moo_positions = {p.ticker: p for p in moomoo.get_positions()}

        # Compare
        all_tickers = set(alp_positions) | set(moo_positions)
        for t in sorted(all_tickers):
            a = alp_positions.get(t)
            m = moo_positions.get(t)
            if a is None or m is None:
                log.warning("position only in one broker: %s alp=%s moo=%s", t, a, m)
                continue
            qty_diff = abs(a.qty - m.qty)
            cost_diff_pct = abs(a.avg_cost - m.avg_cost) / a.avg_cost * 100
            if qty_diff > 0.01 or cost_diff_pct > 1.0:
                log.warning("position diverges: %s qty %.4f vs %.4f, cost %.2f vs %.2f",
                            t, a.qty, m.qty, a.avg_cost, m.avg_cost)
            else:
                log.info("position matches: %s qty=%.4f cost=%.2f", t, a.qty, a.avg_cost)

        # Also reconcile Moomoo activities — feeds into order_execution alongside Alpaca
        reconcile_activities(
            session=session, adapter=moomoo, since=datetime.now(UTC) - timedelta(days=7),
        )
    finally:
        moomoo.close()
```

Schedule:

```python
sched.add_job(
    run_moomoo_parallel,
    trigger=CronTrigger(day_of_week="mon-fri", hour=16, minute=50,
                       timezone="America/New_York"),
    id="moomoo_parallel", misfire_grace_time=60 * 30,
)
```

16:50 ET runs after reconciliation. The job logs but doesn't email — divergences appear in the Friday weekly review's "Moomoo parallel status" section (§4).

### 3d. Parallel-run soak — what to verify before flipping primary

Defined success criteria for at least 4 weekly observations (i.e., 2–4 calendar weeks):

1. Position `qty` matches exactly across both brokers (allowing for fractional-share differences if applicable).
2. Position `avg_cost` matches within 1% (small differences are expected from FX or settlement timing).
3. Account `equity_usd` matches within 0.5% (small mark-to-market differences are normal).
4. Activities reconcile cleanly: every Moomoo deal that should also be an Alpaca order has a corresponding `order_execution` row (or vice versa, depending which is primary).
5. Zero OpenD connection failures requiring manual intervention. If OpenD crashed once and auto-restarted, that's fine. If it required you to kick it twice, that's not soak-complete.

**Phase 4 does not flip `BROKER=moomoo`.** That's a deliberate manual step after the soak, possibly weeks after the tag.

### 3e. ADR-0018 (Moomoo adapter parallel-run protocol)

Document the five success criteria above, plus the operational rules:
- OpenD always on host, never in container.
- Moomoo's free-tier market-data quota is lower than Alpaca; bars stay on Alpaca regardless of BROKER setting (per ADR-0001).
- The Moomoo `client_order_id` field is `remark` in their API — adapt at the boundary.
- Ticker prefix-stripping (`US.AAPL` → `AAPL`) is enforced at the adapter, never leaks to the rest of the app.

---

## 4. Weekly review email (~3 evenings)

### 4a. `jobs/weekly_review.py`

```python
def run_weekly_review(settings, adapter, emailer, llm):
    week_start = monday_of_this_week()
    week_end = friday_of_this_week()

    with session_scope() as s:
        # Section 1: realized PnL
        realized = s.scalars(
            select(func.sum(OrderExecution.realized_pnl_usd))
            .where(OrderExecution.filled_at.between(week_start, week_end))
            .where(OrderExecution.side == "sell")
        ).one() or 0.0

        # Section 2: suggestions vs fills
        weeks_suggestions = s.scalars(
            select(OrderSuggestion).where(OrderSuggestion.week_of == week_start.date())
        ).all()
        # Each suggestion has status: pending | accepted | rejected | expired | filled
        # Build the 3-column table: "suggested" / "user action" / "fill outcome"

        # Section 3: drift state vs targets after week's moves
        current_positions = take_snapshot(adapter, s)
        gap_rows, skipped = compute_gap(s)        # tuple from Phase 3c §1
        drift_alerts = [r for r in gap_rows if r.band_status != "in_band"]

        # Section 4: big events from daily monitor (movers + material news)
        movers_this_week = s.scalars(
            select(NewsEvent).where(NewsEvent.published_at.between(week_start, week_end))
            .where(NewsEvent.llm_material == True).order_by(NewsEvent.published_at.desc())
        ).all()

        # Section 5: next Sunday's preview — run the suggestion engine without persisting
        # (or just reference Sunday's email; preview-only avoids prompt cost)
        preview_drafts = generate_suggestions_preview_only(s, llm)

        # Section 6: Moomoo parallel status (during soak period)
        moomoo_divergences = get_moomoo_divergences_this_week(s)   # from logs persisted to DB

    review = WeeklyReview(
        week_of=week_start, realized_pnl=realized,
        suggestions=weeks_suggestions, drift_alerts=drift_alerts,
        material_movers=movers_this_week, preview=preview_drafts,
        moomoo_divergences=moomoo_divergences,
    )
    html = render_template("weekly_review.html.j2", review=review)
    text = render_template("weekly_review.txt.j2", review=review)
    emailer.send(to=settings.email_to,
                 subject=f"Weekly Review — week of {week_start:%b %d}",
                 html=html, text=text)
```

Cron:

```python
sched.add_job(
    run_weekly_review,
    trigger=CronTrigger(day_of_week="fri", hour=17, minute=0,
                       timezone="America/New_York"),
    id="weekly_review", misfire_grace_time=60 * 60,
)
```

17:00 ET Friday — 15 minutes after the 16:45 reconciliation, so the week's executions are all reconciled by the time the review composes.

### 4b. Email template sections

`templates/weekly_review.html.j2`:

1. **Header** — week-of date, account equity, total realized PnL for the week (green if positive, red if negative).
2. **Suggestions-vs-fills reconciliation** — one row per suggestion this week, three columns: suggested (ticker, side, qty, limit), user action (accepted/rejected/expired by you), fill outcome (filled at $X.XX, partially filled, or expired-with-no-fill). This is the "honest audit trail" the product was built to produce.
3. **Drift state after this week's moves** — current allocation %, target %, band status. Highlight tickers that moved outside their band this week.
4. **Material events this week** — list of `llm_material=true` news items per held ticker, ordered by date. Brief summary from the news_event row.
5. **Next Sunday preview** — short list of likely suggestions (the engine running without persistence so the user knows what's coming). Caveat: this is a preview, the real Sunday email will run again on fresh data.
6. **Moomoo parallel status** — during the parallel-run period: position/account divergences observed this week, with green checkmarks for "matched" and yellow warnings for "diverged." After Moomoo is flipped to primary, this section can be removed.

### 4c. ADR-0019 (Weekly review composition)

Documents the section order, the data sources, the cadence rationale (Friday review = reflection + preview; Sunday email = action), and the Moomoo-section sunset criteria (remove from the template after the primary flip).

---

## 5. Opt-in auto-trade execution (~1 week + 14-week staged soak)

This is the workstream that was originally Phase 4.6. It's folded in here because (a) its data model is `order_execution` which Phase 4 already builds, (b) its wash-sale guard reads from reconciliation history that Phase 4 already produces, and (c) the order-of-operations between auto-trade and reconciliation needs to be designed as one system, not two.

### 5a. Additional schema (Alembic)

```python
def upgrade():
    # auto_trade_mode lives in meta so it can be mutated atomically by the
    # promotion endpoint without an .env edit. Single row, key='auto_trade_mode',
    # value in {'OFF','DRY_RUN','LIVE'}. (meta table already exists from Phase 0.)

    op.create_table(
        "auto_trade_promotion_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_mode", sa.String, nullable=False),
        sa.Column("to_mode", sa.String, nullable=False),
        sa.Column("broker_scope", sa.String, nullable=False),     # alpaca_paper|alpaca_live|moomoo
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("actor", sa.String, nullable=False),            # human or "kill_switch" or "guard_failure"
    )
    op.create_table(
        "kill_switch_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String, nullable=False),          # "manual" | "cap_breach" | "readback_mismatch" | "broker_error"
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("cancelled_order_ids", sa.JSON, nullable=True), # list of broker order IDs that were cancelled
    )
    op.create_table(
        "auto_trade_caps",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("per_order_max_usd", sa.Float, nullable=False),
        sa.Column("per_day_max_usd", sa.Float, nullable=False),
        sa.Column("per_week_max_usd_per_ticker", sa.Float, nullable=False),
        sa.Column("per_day_max_orders", sa.Integer, nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
    )
```

`order_execution.dry_run` and `order_execution.client_order_id` are already added in §2's migration. No additional column work here.

Seed the initial caps row conservatively (these are starting numbers; update via promotion endpoint, never by direct edit):

```sql
INSERT INTO auto_trade_caps
  (per_order_max_usd, per_day_max_usd, per_week_max_usd_per_ticker,
   per_day_max_orders, effective_from)
VALUES (500, 1500, 1000, 5, datetime('now'));
```

### 5b. `BrokerAdapter.submit_order` extension

The `BrokerAdapter` protocol currently has `submit_order_draft` only. Phase 4 extends with the real `submit_order` — and this is the *only* protocol change for write capability.

```python
# brokers/base.py
@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str            # MUST be supplied; format f"sug-{suggestion.id}"
    ticker: str
    side: Literal["buy", "sell"]
    qty: float
    limit_price: float | None       # None = market order (Phase 4 v1 only uses limit)
    time_in_force: Literal["day", "gtc"] = "day"

@dataclass(frozen=True)
class OrderConfirmation:
    broker_order_id: str
    client_order_id: str
    status: str                     # accepted_for_routing | filled | rejected | ...
    submitted_at: datetime

class BrokerAdapter(Protocol):
    # ... existing methods
    def submit_order(self, req: OrderRequest) -> OrderConfirmation: ...
    def get_order(self, broker_order_id: str) -> OrderConfirmation: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
```

Alpaca implementation in `brokers/alpaca.py`:

```python
def submit_order(self, req: OrderRequest) -> OrderConfirmation:
    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    order = self._client.submit_order(
        order_data=LimitOrderRequest(
            symbol=req.ticker, qty=req.qty,
            side=OrderSide.BUY if req.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY if req.time_in_force == "day" else TimeInForce.GTC,
            limit_price=req.limit_price,
            client_order_id=req.client_order_id,
        )
    )
    return OrderConfirmation(
        broker_order_id=str(order.id),
        client_order_id=order.client_order_id,
        status=str(order.status),
        submitted_at=order.submitted_at or datetime.now(UTC),
    )
```

Moomoo implementation mirrors via OpenD; `client_order_id` maps to Moomoo's `remark` field per ADR-0018.

### 5c. `services/auto_trade.py` — the core module

The *only* file in the codebase outside `brokers/` permitted to invoke `submit_order`. Enforced by `tests/test_no_unauthorized_submit_order.py` (a grep test that fails CI if any other file imports/calls `submit_order`).

```python
# src/investor/services/auto_trade.py
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Literal

from sqlalchemy.orm import Session
from sqlalchemy import select, func
from investor.brokers.base import BrokerAdapter, OrderRequest, OrderConfirmation
from investor.models import (
    OrderSuggestion, OrderExecution, KillSwitchLog, AutoTradeCaps, Meta,
)
from investor.services.email import EmailSender

log = logging.getLogger(__name__)
Mode = Literal["OFF", "DRY_RUN", "LIVE"]


@dataclass(frozen=True)
class AutoTradeOutcome:
    suggestion_id: int
    placed: bool
    dry_run: bool
    broker_order_id: str | None
    rejected_reason: str | None


class _GuardFailure(Exception):
    """Per-suggestion guard rejection. Does NOT flip mode; just skips this one."""


def run_auto_trade_pass(
    session: Session, adapter: BrokerAdapter, emailer: EmailSender,
) -> list[AutoTradeOutcome]:
    """Polls accepted-but-not-yet-executed suggestions; auto-trades per mode + caps.

    Idempotent — running this twice over the same accepted suggestion does nothing
    on the second call (idempotency check via client_order_id uniqueness on
    order_execution).
    """
    mode = _get_mode(session)
    if mode == "OFF":
        return []

    caps = _get_active_caps(session)
    accepted = _fetch_accepted_unexecuted(session)
    outcomes: list[AutoTradeOutcome] = []

    for sug in accepted:
        try:
            _check_idempotency(session, sug)
            _check_wash_sale(session, sug)
            _check_caps(session, sug, caps, accepted_today=outcomes)
            _check_cash_sufficiency(session, sug, adapter)
        except _GuardFailure as gf:
            outcomes.append(AutoTradeOutcome(
                suggestion_id=sug.id, placed=False,
                dry_run=(mode == "DRY_RUN"),
                broker_order_id=None, rejected_reason=str(gf),
            ))
            continue

        cid = f"sug-{sug.id}"
        req = OrderRequest(
            client_order_id=cid, ticker=sug.ticker, side=sug.side,
            qty=sug.qty, limit_price=sug.limit_price,
        )

        if mode == "DRY_RUN":
            # Simulate. Write order_execution with dry_run=true. Never call broker.
            session.add(OrderExecution(
                suggestion_id=sug.id, broker_order_id=None, client_order_id=cid,
                ticker=sug.ticker, side=sug.side,
                submitted_qty=sug.qty, filled_qty=0,
                limit_price=sug.limit_price, filled_price=None,
                broker=_current_broker_name(),
                status="dry_run",
                dry_run=True,
                match_method="auto_trade_placed", match_confidence=1.0,
                created_at=datetime.now(UTC),
            ))
            session.commit()
            outcomes.append(AutoTradeOutcome(
                suggestion_id=sug.id, placed=True, dry_run=True,
                broker_order_id=None, rejected_reason=None,
            ))
            continue

        # mode == "LIVE": real broker call
        try:
            conf = adapter.submit_order(req)
        except Exception as e:
            _trigger_kill_switch(session, emailer, adapter,
                                 trigger="broker_error", detail=str(e))
            raise

        # Read-back within 60 seconds — verify client_order_id round-tripped
        try:
            conf2 = adapter.get_order(conf.broker_order_id)
            if conf2.client_order_id != cid:
                _trigger_kill_switch(session, emailer, adapter,
                                     trigger="readback_mismatch",
                                     detail=f"client_order_id mismatch on {conf.broker_order_id}")
                raise RuntimeError("readback mismatch")
        except Exception as e:
            _trigger_kill_switch(session, emailer, adapter,
                                 trigger="readback_failed", detail=str(e))
            raise

        # Persist immediately — reconciliation at 16:45 ET will UPDATE this row
        # with fill data when the broker activity poll picks up the fill.
        session.add(OrderExecution(
            suggestion_id=sug.id,
            broker_order_id=conf.broker_order_id, client_order_id=cid,
            ticker=sug.ticker, side=sug.side,
            submitted_qty=sug.qty, filled_qty=0,
            limit_price=sug.limit_price, filled_price=None,
            broker=_current_broker_name(),
            status="accepted_for_routing",
            dry_run=False,
            match_method="auto_trade_placed", match_confidence=1.0,
            created_at=conf.submitted_at,
        ))
        session.commit()
        outcomes.append(AutoTradeOutcome(
            suggestion_id=sug.id, placed=True, dry_run=False,
            broker_order_id=conf.broker_order_id, rejected_reason=None,
        ))

    return outcomes
```

The guard helpers (`_check_idempotency`, `_check_wash_sale`, `_check_caps`, `_check_cash_sufficiency`, `_trigger_kill_switch`, `_get_mode`, `_get_active_caps`) each get their own 10–20 line implementation. Each raises `_GuardFailure` with a human-readable reason on per-suggestion rejection; only cap breach and read-back mismatch trigger the kill switch.

**`_check_wash_sale`** is the most important guard — reads from `order_execution` where `dry_run = false AND realized_pnl_usd < 0 AND ticker = sug.ticker AND filled_at >= now - INTERVAL '30 days'` (calendar days). If any matching row exists, the buy is blocked with reason `"wash_sale_guard: loss-sale of {qty} {ticker} on {date}"`. The `dry_run = false` filter is critical — DRY_RUN simulated losses must never block real buys.

**`_check_idempotency`** queries `order_execution.client_order_id` for `f"sug-{sug.id}"` — if any row exists for this suggestion (dry-run or live), skip. Same suggestion can't be placed twice.

**`_trigger_kill_switch`** writes `kill_switch_log`, flips `meta` row `auto_trade_mode='OFF'`, calls `adapter.cancel_order` on every open auto-trade-placed order from the last 24 hours, sends an alert email.

### 5d. Scheduler integration — 9:35 ET cron

```python
sched.add_job(
    run_auto_trade_pass_wrapper,
    trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=35,
                       timezone="America/New_York"),
    id="auto_trade",
    misfire_grace_time=60 * 15,
)
```

**9:35 ET** — 5 minutes after market open. Limit orders placed at this time have the full day to fill or expire (`time_in_force=day`). Auto-trade fires *before* reconciliation (which is 16:45 ET), so the day's flow is:

```
 Sun 18:00 ET: Phase 3c review → order_suggestion(status='pending')
 Sun 18:30+:   User clicks Accept → status='accepted', acted_at=now
 Mon 09:35 ET: Auto-trade fires (this section)
              → INSERT order_execution(status='accepted_for_routing', dry_run=…)
 Mon 09:35–Fri 16:00: order fills (or doesn't) at broker
 Daily 16:45 ET: Reconciliation polls activities
              → UPDATE order_execution rows by broker_order_id
                (filled_qty, filled_price, filled_at, status, realized_pnl)
              → INSERT new rows for manual trades auto-trade didn't track
 Fri 17:00 ET: Weekly review reads now-complete order_execution rows
```

Also expose `POST /admin/run-auto-trade` for manual testing. Requires `X-Admin-Token`.

### 5e. Promotion endpoint + admin commands

Promoting the mode is **not** a config-file edit. It's a deliberate command authenticated by a separate `AUTO_TRADE_PROMOTION_TOKEN`.

```python
class PromoteMode(BaseModel):
    to_mode: Literal["OFF", "DRY_RUN", "LIVE"]
    broker_scope: Literal["alpaca_paper", "alpaca_live", "moomoo"]
    reason: str

@app.post("/admin/auto-trade/promote",
         dependencies=[Depends(auto_trade_promotion_auth)])
def promote_mode(body: PromoteMode):
    """Requires AUTO_TRADE_PROMOTION_TOKEN (separate from ADMIN_TOKEN)."""
    with session_scope() as s:
        current = _get_mode(s)
        current_scope = _get_broker_scope(s)
        _validate_promotion(current, current_scope, body)
        _set_mode(s, body.to_mode)
        _set_broker_scope(s, body.broker_scope)
        s.add(AutoTradePromotionLog(
            ts=datetime.now(UTC), from_mode=current, to_mode=body.to_mode,
            broker_scope=body.broker_scope, reason=body.reason, actor="admin",
        ))
        s.commit()
    return {"mode": body.to_mode, "broker_scope": body.broker_scope}


def _validate_promotion(current: Mode, current_scope: str, body: PromoteMode):
    """Enforce soak windows. Demote-to-OFF is always allowed."""
    if body.to_mode == "OFF":
        return
    soak_days_required = {
        ("alpaca_paper", "DRY_RUN"):  14,
        ("alpaca_paper", "LIVE"):     28,
        ("alpaca_live",  "LIVE"):     28,
        ("moomoo",       "LIVE"):     28,
    }
    key = (body.broker_scope, body.to_mode)
    required = soak_days_required.get(key, 0)
    if required:
        last_change = _last_promotion_log_for(body.broker_scope)
        if last_change is None or (datetime.now(UTC) - last_change.ts).days < required:
            raise HTTPException(409, f"soak window not satisfied: need {required} days in current mode")
```

Demoting to `OFF` is always allowed and instant. Promoting upward enforces soak windows — you cannot go from "first DRY_RUN today" to "LIVE on Moomoo tomorrow" no matter how confident you feel.

Caps update endpoint similar: `POST /admin/auto-trade/caps` with the new values plus a reason. Old caps row gets `effective_to=now`; new row inserted.

Kill switch: `POST /admin/auto-trade/emergency-stop` (`ADMIN_TOKEN` sufficient — the higher-trust `AUTO_TRADE_PROMOTION_TOKEN` is only for promotion *up*). Flips mode to `OFF`, cancels open auto-trade orders, logs to `kill_switch_log`.

### 5f. Daily auto-trade summary email

After every `run_auto_trade_pass`, email a brief summary:

```
Subject: Auto-Trade — 2026-06-15 — 3 placed, 1 rejected (DRY_RUN)

Mode: DRY_RUN | Broker: alpaca_paper

Placed:
  • BUY 10 VOO @ $487.20 (sug-145) — confidence 0.78
  • BUY 5 SCHD @ $79.15 (sug-146) — confidence 0.82
  • BUY 3 AAPL @ $182.40 (sug-147) — confidence 0.71

Rejected:
  • SELL 2 MSFT (sug-144) — wash_sale_guard: loss sale 18 days ago

Caps: per-order $500, per-day $1500, per-week-per-ticker $1000
Today's spend: $1,234.55 (82% of daily cap)

[View order_execution audit trail] [Disable auto-trade]
```

The "Disable auto-trade" link is a magic-link that flips mode to `OFF` — the kill switch for when you read an email and realise something's wrong. When mode is `LIVE`, the daily summary should be the *first* thing you read each morning during the soak.

### 5g. Weekly review email gains an auto-trade section

In §4's weekly review email, add a seventh section between "next-Sunday preview" and "Moomoo parallel status":

- Auto-trade activity this week: mode at start vs end, any promotions, any kill-switch events, any cap breaches, total placed vs total rejected with reasons.
- During DRY_RUN soak weeks, this section is also where you spot-check whether the "would have placed" decisions look sensible. If you'd be uncomfortable with a real placement, that's a signal to delay LIVE promotion.

### 5h. The accept-link confirmation step changes when mode != OFF

In Phase 3a, clicking Accept on a suggestion in the weekly email flipped `order_suggestion.status='accepted'` and showed a generic confirmation page. In Phase 4 with auto-trade enabled, the confirmation page must change behavior based on mode:

- **`OFF`**: Existing behaviour. "Suggestion accepted. Place the order manually in your broker."
- **`DRY_RUN`**: "Suggestion accepted. Auto-trade will simulate placement at 9:35 AM ET tomorrow. Watch your inbox for the dry-run confirmation."
- **`LIVE`**: "Suggestion accepted. **AUTO-TRADE LIVE** — order will be placed at 9:35 AM ET tomorrow at $X.XX limit. Click here to reject and stop the auto-trade." The "reject" link flips status back to `pending`, which auto-trade ignores. This is the second user-confirmation step before real money moves.

---

## 6. Smoke-test checklist (multi-tag — see DoD table at the top)

The checklist is split into three blocks. **Block A** (rows 1–18) gates `v0.4.0-phase-4-code-complete`. **Block B** (rows 19–27) gates the auto-trade promotion tags `v0.4.1` through `v0.4.4`. **Block C** is the per-promotion soak-window observations (one row per tag).

**Block A — code-complete (`v0.4.0-phase-4-code-complete`):**

| # | Step | Pass criteria |
|---|---|---|
| 1 | `uv run alembic upgrade head` | `order_execution` (with `dry_run`, `client_order_id`), `auto_trade_promotion_log`, `kill_switch_log`, `auto_trade_caps` all exist. `meta.auto_trade_mode = 'OFF'` after migration. |
| 2 | `uv run pytest -m "not integration"` | All tests pass; total ≥ 240 (up from 189 at Phase 3c close — reconciliation + auto-trade adds ~50 tests) |
| 3 | Phase 3c carryover #1 fix — scoring failure surfaced (regression for MU pattern) | `tests/test_weekly_suggestions.py::test_scoring_failure_surfaced` passes; log shows traceback, email annotation present |
| 4 | `AlpacaAdapter.get_activities()` returns recent FILLs | Manual paper trade → activity appears in adapter output within 1 minute |
| 5 | `MoomooAdapter.get_positions()` returns positions from OpenD paper account | OpenD running on host; container can reach `host.docker.internal:11111`; positions match what's visible in the Moomoo app |
| 6 | `MoomooAdapter` ticker prefix-stripping | Moomoo returns `US.AAPL`; adapter output is `AAPL` |
| 7 | Reconciliation Rule 1 — auto-trade-inserted row gets UPDATE-ed | Auto-trade in DRY_RUN inserts an `order_execution` with `broker_order_id=NULL` and `dry_run=true`; reconciliation correctly ignores it (DRY_RUN filter). Auto-trade in LIVE inserts row with `broker_order_id`; reconciliation's matcher finds existing row by broker_order_id and UPDATEs `filled_qty`/`filled_price`/`status` rather than inserting a duplicate |
| 8 | Reconciliation heuristic match (Rule 2) | A pending suggestion for VOO buy at $487.20 + a fill at $487.15 within 24h placed manually → auto_matched, confidence 0.9 |
| 9 | Reconciliation flags ambiguous case (Rule 3) | Two pending suggestions for same ticker+side within window; the algorithm picks one but flags `match_method=manual_review` |
| 10 | Reconciliation flags untracked (Rule 4) | Manually trade SPY in Alpaca paper without any matching suggestion; activity logged as `match_method=untracked` |
| 11 | `POST /admin/reconcile/{id}` updates a manual_review row | Admin token required; flips `match_method=manual_matched`, populates suggestion_id |
| 12 | Idempotency — re-run reconciliation over same window | No duplicate rows (unique constraint enforced); UPDATE-with-same-values is a no-op |
| 13 | Realized PnL on a sell — FIFO cost basis | Seed `order_execution` with a buy at $100 (5 shares) and a sell at $110 (5 shares); realized_pnl_usd = $50 |
| 14 | Moomoo parallel-run job logs divergences | Inject a Moomoo position with different qty than Alpaca; log shows WARNING with both values |
| 15 | Friday 17:00 ET — weekly review email arrives | All seven sections render (the auto-trade section from §5g is the seventh); HTML + plain text both readable |
| 16 | Suggestions-vs-fills table reads honestly | A suggestion that you rejected shows "rejected"; an accepted+filled one shows fill price; an accepted+expired one shows "expired (no fill)" |
| 17 | Moomoo parallel status section renders during soak | If divergences exist this week, yellow warnings appear; clean week → green checkmark |
| 18 | First green parallel-run week (positions match, activities reconcile) | Soak-window day 1 of N; eligible for primary flip after 4+ such weeks |

**Block B — auto-trade framework, runnable as soon as code-complete:**

| # | Step | Pass criteria |
|---|---|---|
| 19 | Default-OFF invariant | Fresh `alembic upgrade head` → `meta.auto_trade_mode='OFF'`. Even with all 4 caps set, with no promotion command, the 9:35 cron does nothing. |
| 20 | Mode promotion + soak validation | `POST /admin/auto-trade/promote {to_mode: DRY_RUN, broker_scope: alpaca_paper}` succeeds with `AUTO_TRADE_PROMOTION_TOKEN`. Same request without it → 401. Attempting `paper LIVE` 1 day after entering `DRY_RUN` → 409 ("soak window not satisfied: need 14 days"). |
| 21 | Demote to OFF is always allowed | From any state, demote-to-OFF returns 200 instantly. No soak gate. |
| 22 | DRY_RUN end-to-end | Accept a suggestion; manually trigger `POST /admin/run-auto-trade`; row appears in `order_execution` with `dry_run=true, broker_order_id=NULL, status='dry_run', match_method='auto_trade_placed'`. No broker call (verify with adapter mock). |
| 23 | LIVE end-to-end with read-back | Switch to `LIVE` (with soak override for testing); accept a suggestion; auto-trade calls `adapter.submit_order` → `get_order` for read-back → INSERTs `order_execution` with `status='accepted_for_routing'`. Same-day 16:45 reconciliation UPDATEs to `status='filled'`. |
| 24 | Idempotency — same suggestion can't be placed twice | Accept a suggestion; run auto-trade twice; second run sees existing `order_execution` with `client_order_id='sug-N'` and skips (no second broker call). |
| 25 | Wash-sale guard blocks | Seed `order_execution` with `side='sell', realized_pnl_usd=-50, ticker='AAPL', filled_at=20 days ago`. Auto-trade attempts to buy AAPL → blocked with `rejected_reason='wash_sale_guard'`. DRY_RUN losses with `dry_run=true` do NOT block. |
| 26 | Hard caps flip to OFF and alert | Set per-day cap to $50; accept a $200 suggestion; auto-trade rejects + flips mode to `OFF` + writes `kill_switch_log` row + sends alert email. |
| 27 | Read-back mismatch flips to OFF | Mock adapter so `submit_order` returns `broker_order_id=X` but `get_order(X)` returns `client_order_id="bogus"`. Mode flips to `OFF`, kill_switch_log row written, alert sent. |
| 28 | Kill switch endpoint | `POST /admin/auto-trade/emergency-stop` → mode `OFF`, all open auto-trade-placed orders cancelled at broker, `kill_switch_log` row inserted. |
| 29 | Static import check | `grep -rn 'submit_order' src/ --include='*.py' | grep -v 'services/auto_trade.py' | grep -v 'brokers/'` returns zero matches. CI-enforced. |

**Block C — promotion tags (one row per tag, observed in calendar time):**

| Tag | Observation that gates it |
|---|---|
| `v0.4.1-paper-dry-run` | 2 weeks of `DRY_RUN` on Alpaca paper. Daily summary emails show plausible would-have-placed decisions; you'd be comfortable with each as a real order; zero kill-switch events. |
| `v0.4.2-paper-live` | 4 weeks of `LIVE` on Alpaca paper. Read-back succeeds on every order; reconciliation correctly transitions `accepted_for_routing` → `filled` (or partial / expired); wash-sale guard fired at least once and was correct; zero unexpected kill-switch events. |
| `v0.4.3-alpaca-live` | 4 weeks of `LIVE` on real Alpaca with small capital. All Block C criteria met for paper-LIVE, but on real money. Daily summary emails read every morning during soak. |
| `v0.4.4-moomoo-live` | Moomoo primary flip (`BROKER=moomoo`) clean for 1+ week + 4 weeks of `LIVE` on Moomoo after promotion. Includes Moomoo-specific edge cases (ticker prefix, `remark`-as-client-order-id mapping, OpenD session reliability). |

Tag and push each as criteria are met:

```bash
# After Block A passes:
git add -A && git commit -m "phase 4 code complete: reconciliation, moomoo adapter, weekly review, auto-trade (OFF default)"
git tag v0.4.0-phase-4-code-complete && git push --tags

# Subsequent tags as each soak window closes cleanly:
git tag v0.4.1-paper-dry-run    && git push --tags
git tag v0.4.2-paper-live       && git push --tags
git tag v0.4.3-alpaca-live      && git push --tags
git tag v0.4.4-moomoo-live      && git push --tags
```

The code-complete tag is reachable in ~3.5 weeks of focused work. The final tag is roughly 14–16 weeks of calendar time later. **No promotion happens automatically** — each tag corresponds to an explicit admin promotion command + a clean soak observation.

---

## 7. Distance-guard calibration revisit (Phase 3c carryover #2)

Phase 3c bumped `max_distance_pct` from 8% → 15% to unblock MU. After 2–3 weeks of Phase 4 reconciliation data, you have the inputs to revisit whether 15% should stick.

Run this analysis once you have at least 20 reconciled buy executions:

```sql
SELECT
  CASE
    WHEN ABS(oe.filled_price - os.limit_price) / os.limit_price * 100 < 2 THEN '0-2%'
    WHEN ABS(oe.filled_price - os.limit_price) / os.limit_price * 100 < 5 THEN '2-5%'
    WHEN ABS(oe.filled_price - os.limit_price) / os.limit_price * 100 < 8 THEN '5-8%'
    WHEN ABS(oe.filled_price - os.limit_price) / os.limit_price * 100 < 15 THEN '8-15%'
    ELSE '15%+'
  END AS distance_bucket,
  COUNT(*) AS total,
  SUM(CASE WHEN oe.status = 'filled' THEN 1 ELSE 0 END) AS filled,
  ROUND(100.0 * SUM(CASE WHEN oe.status = 'filled' THEN 1 ELSE 0 END) / COUNT(*), 1) AS fill_rate_pct
FROM order_execution oe
JOIN order_suggestion os ON oe.suggestion_id = os.id
WHERE oe.side = 'buy'
GROUP BY 1 ORDER BY 1;
```

If suggestions in the 8–15% bucket consistently fill, 15% is justified. If they expire-without-fill at a much higher rate than 0–8%, 15% is too permissive and you should either tighten the global guard or introduce per-ticker overrides (e.g., volatile individual names get 15%, ETFs stay at 8%).

This analysis is a one-time decision point, not an ongoing job. Document the choice with a date in ADR-0007 and move on.

---

## 8. Common Phase 4 pitfalls

1. **OpenD in Docker.** Don't try. Moomoo has no official Linux build, and the Wine-based ports are unstable. OpenD must run on the host, and the Docker container reaches it via `host.docker.internal:11111`. Same pattern as ADR-0001 documented from Phase 0.
2. **OpenD bind address.** Out of the box OpenD binds to `127.0.0.1:11111` which is unreachable from Docker. Change the bind to `0.0.0.0:11111` *on the host* — Docker's `host.docker.internal` resolves the host's interface, and OpenD must be listening there.
3. **Ticker prefix leaks.** Moomoo returns `US.AAPL`, `HK.0700`. Strip at the adapter boundary. If you ever see `US.AAPL` in `order_suggestion.ticker`, the adapter has a bug.
4. **`client_order_id` field name.** Alpaca calls it `client_order_id`. Moomoo stores user-supplied IDs in `remark`. Map at the adapter; the rest of the app uses one consistent name.
5. **futu-api is sync, not async.** No async wrapper needed — `MoomooAdapter` is sync like `AlpacaAdapter`. APScheduler jobs are thread-friendly so this just works.
6. **Activities pagination.** Both Alpaca and Moomoo paginate activities. The default page size is 100; reconciliation pulling 7 days of history can exceed that if you trade a lot. Add explicit pagination handling in `get_activities` — accumulate until exhausted.
7. **Realized PnL on partial fills.** A buy of 10 shares filled as 7+3 over two days, then a sell of 10. FIFO matches the 7 first (older buy lot), then the 3 (newer). The cost basis is qty-weighted across the two fills. Don't naively use the average buy price.
8. **Activities since-window drift.** If reconciliation runs at 16:45 ET daily and the broker's activity timestamps are in UTC (Alpaca) or HKT (Moomoo), an off-by-timezone bug can miss activities from the trailing hour. Always pull `since = last_run_at - 1 hour` to overlap, and rely on the unique constraint to dedup.
9. **Wash-sale window definition.** US tax rules: 30 *calendar* days before and after a loss-sale. Not trading days. Document this in ADR explicitly because future-you will second-guess.
10. **Moomoo deal vs order.** Moomoo's `deal_list_query` returns *fills* (deals); `order_list_query` returns *orders*. Reconciliation cares about deals (what actually filled), not orders (what was submitted). The Moomoo adapter's `get_activities` uses `deal_list_query`.
11. **Friday-review preview is not authoritative.** The "next Sunday preview" section in the weekly review runs the suggestion engine without persisting. The actual Sunday email may show different suggestions because (a) bar data may shift between Friday close and Sunday evening, (b) the LLM-review pipeline is non-deterministic at the prompt level. Document this clearly in the email so the user doesn't take Friday's preview as a commitment.
12. **Moomoo OpenD session expiry.** OpenD's authenticated session can expire after some idle period (typically 24 hours). The parallel-run job should detect a failed call, attempt a single reconnect, and only then raise. Wrap `_trade_ctx`/`_quote_ctx` in a thin retry layer.
13. **Reconciliation gaps undermine the wash-sale guard.** Auto-trade reads `order_execution` for its wash-sale guard. If reconciliation has gaps (e.g., a 3-day OpenD outage where activities weren't reconciled), the guard's input is incomplete and could let through buys that would actually be wash-sale violations. Add a `data_coverage_check` admin endpoint that flags reconciliation gaps and refuses to enable auto-trade `LIVE` if any exist for the last 30 calendar days.
14. **Auto-trade default-OFF lives in the `meta` table, not env vars.** Adding `auto_trade_mode` to `Settings`/`.env` would let it get accidentally promoted by an env edit. Resist the temptation. The mode is mutated only by `POST /admin/auto-trade/promote`.
15. **Idempotency race condition.** Two concurrent auto-trade passes (e.g., `POST /admin/run-auto-trade` while the 9:35 cron fires) could both see "no existing order_execution for sug-N" and both submit. Mitigation: SQLite `BEGIN IMMEDIATE` transaction wrapping the idempotency check + the order placement + the INSERT. Test row 24 covers.
16. **`time_in_force=day` and late-day caution.** Limit orders placed near close don't fill. The 9:35 ET cron prevents this. If you ever introduce an afternoon auto-trade firing, switch to `gtc` with an explicit expiry — but think hard first, GTC orders can fill weeks after the user's intent expires.
17. **Promotion soak window measured from wrong event.** The required soak is N days *in the new mode without incident*, not N days since the auto-trade module was deployed. `_validate_promotion` reads from `auto_trade_promotion_log` for the last successful promotion *to this mode for this broker scope* — verify the query matches that intent.
18. **Kill-switch cancellation race.** When the kill switch fires, an order placed seconds ago may not yet have a `broker_order_id` to cancel. Pattern: kill switch reads from `order_execution.broker_order_id` rows from the last 24h *plus* any in-flight tracked submissions. Accept that a recently-placed order might not get cancelled if the broker hasn't acked yet.
19. **Daily cap reset boundary.** Caps are "per day" but "day" can mean UTC, ET, or calendar day. Pick ET (matches market hours), document, and write a test that an order at 23:59 ET counts in today's bucket and 00:01 ET counts in tomorrow's.
20. **`AUTO_TRADE_PROMOTION_TOKEN` leak.** Must never appear in logs, error messages, or audit trails. The promotion endpoint accepts it as a header and does not echo it back anywhere.
21. **DRY_RUN losses must NOT block real buys.** Phase 4's wash-sale guard filters `WHERE dry_run = false`. If you ever change the wash-sale query, re-verify this filter — it's the single line of code that prevents simulated losses from polluting real-mode decisions. CI test row 25 enforces.
22. **Reconciliation rule mismatch.** Rule 1 (auto-trade-inserted row) is checked first; Rule 2 (heuristic match) runs only if Rule 1 didn't apply. If a manual trade happens to use a `client_order_id` matching the `sug-N` format, Rule 1 will incorrectly try to update a non-existent auto-trade row → fail-soft → falls through to Rule 2. Document the `sug-N` namespace as reserved for the auto-trade module.

---

## 9. ADRs to write in Phase 4

- **`docs/adr/0014-auto-trade-mode-discipline.md`** — new. Three-state mode (`OFF` / `DRY_RUN` / `LIVE`), default-OFF invariant, single-call-site rule (only `services/auto_trade.py` may invoke `submit_order`), promotion soak-window matrix, idempotency via `client_order_id = f"sug-{suggestion.id}"`, read-back-within-60s rule, single-user-only forever (Phase 5 multi-tenant removes LIVE mode entirely per `product_plan.md` mandate).
- **`docs/adr/0015-kill-switch-design.md`** — new. Triggers (manual / cap_breach / readback_mismatch / broker_error); per-suggestion guard rejection (e.g., wash_sale_guard) does *not* trigger the kill switch — it just skips that suggestion. Recovery semantics: kill switch always lands the mode in `OFF`; manual re-promotion required to leave. Cancellation race acceptance. Logged to `kill_switch_log` forever.
- **`docs/adr/0017-reconciliation-matching.md`** — new. The four matching rules in priority order (Rule 1 = upsert auto-trade row by `broker_order_id`; Rule 2 = heuristic match within 48h time + 0.5% price tolerance; Rules 3, 4 unchanged); the FIFO cost-basis algorithm for realized PnL; partial-fill handling; the `sug-N` `client_order_id` namespace reservation. Anchor for any future enhancement to the matcher.
- **`docs/adr/0018-moomoo-parallel-run.md`** — new. The five success criteria for the soak period; the rule that bars stay on Alpaca regardless of `BROKER` setting (per ADR-0001 market-data separability); the operational rules (OpenD on host, ticker prefix-stripping at adapter, `remark`-as-`client_order_id` mapping). Establishes the protocol for any future broker addition.
- **`docs/adr/0019-weekly-review-composition.md`** — new. The seven email sections (six original + the auto-trade section from §5g); the rationale for Friday-review vs Sunday-suggestions cadence split (reflection vs action); the Moomoo-section sunset criteria. Brief.
- **`docs/adr/0007-position-sizing.md`** — *update*. Add a section with the date-stamped distance-guard calibration result from §7 (whether 15% stayed or got tightened, and the data that justified the decision).

Five new ADRs, one update. About 3 hours total. ADRs 0014 and 0015 are the most consequential — they document the discipline that keeps auto-trade safe.

---

## 10. Documentation drift to fix

- **CLAUDE.md** — add new architecture convention #14: "**Reconciliation is matching, not creation.** `services/reconciliation.py` writes `order_execution` rows by matching broker activities to existing `order_suggestion` rows. It never invents executions that don't correspond to real broker fills. The four matching rules and their priority order are fixed in ADR-0017; do not add new heuristics without an ADR amendment." Add to common gotchas: OpenD bind address (`0.0.0.0:11111` not `127.0.0.1`), Moomoo ticker prefix-stripping, `client_order_id` ↔ `remark` mapping, wash-sale window is calendar days. Update env vars to include `OPEND_HOST`, `OPEND_PORT`, `OPEND_SECURITY_FIRM`. Add `brokers/moomoo.py`, `services/reconciliation.py`, `jobs/weekly_review.py`, `jobs/moomoo_parallel.py` to repo layout.
- **`product_plan.md`** — when Phase 4 ships, mark it complete with the standard "code complete / tag deferred until first Friday review email + 4 clean parallel-run weeks" pattern. Note that Moomoo primary-flip remains a manual decision after the soak, not gated by the Phase 4 tag.
- **ADRs index** — add entries for 0017, 0018, 0019. Note the update on 0007.

---

## 11. What Phase 4 deliberately does not include

- **Moomoo primary flip (`BROKER=moomoo`).** A separate manual decision after the soak. Phase 4 tag ships with `BROKER=alpaca_paper` or `BROKER=alpaca_live`; the flip is a deliberate later step.
- **Auto-trading of any kind.** That's Phase 4.6 — depends on `order_execution` from this phase but adds the broker-side write capability under a separate three-state mode gate.
- **Tax-lot reporting / Form 8949 generation.** Reconciliation captures realized PnL with FIFO cost basis, which is the underlying data — but assembling a Schedule D-ready report is its own thing and not in scope.
- **Multi-account / multi-broker simultaneously.** The reconciliation can read from one broker per run. If you want to run Alpaca and Moomoo simultaneously as *primary* (not parallel), that's a different architecture and out of scope.
- **Real-time fill notifications.** Reconciliation runs daily at 16:45 ET. If you want immediate notification when a limit fills mid-day, that's webhook integration which is Phase 5+ territory.

---

*When all 29 smoke-test rows (Blocks A + B) are green, you've received one Friday weekly review email with the full seven-section structure (including the auto-trade section), the Moomoo parallel-run has logged at least one clean week of comparisons, and ADRs 0014, 0015, 0017, 0018, 0019 are committed plus 0007 updated, Phase 4 is **code-complete**. Tag `v0.4.0-phase-4-code-complete`. The four promotion tags (`v0.4.1` paper-dry-run → `v0.4.2` paper-live → `v0.4.3` alpaca-live → `v0.4.4` moomoo-live) follow as each soak window closes cleanly in calendar time. Phase 4 isn't truly "done" until `v0.4.4`, but every tag along the way is a real milestone.*
