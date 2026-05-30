# Phase 4.9a — Multi-Broker Plumbing + Per-Broker Reports: Step-by-Step Guide

**Goal:** Refactor the data model so a single user can hold positions across multiple broker accounts simultaneously — starting with Alpaca + Moomoo and adding **IBKR + Tiger**. Add `broker_account_id` to every per-account table; build IBKR and Tiger adapters behind the existing `BrokerAdapter` Protocol; daily and weekly reports become *separate* emails per broker (the consolidated household view is deferred to 4.9b). Suggest-only across all brokers; auto-trade LIVE stays Alpaca-only — each new broker has to repeat its own Phase 4.6 OFF → DRY_RUN → LIVE soak ladder before LIVE trading is enabled, which is deliberately out of scope here.

This is the first of two Phase 4.9 sub-phases. Phase 4.9b layers household targets, the consolidated summary, funds-added detection, quarterly/annual review crons, and magic-link confirmation for large target shifts on top.

**Out of scope for Phase 4.9a:**

- **Household target allocation** and the consolidated summary email — Phase 4.9b.
- **Funds-added detection, quarterly/annual review crons, magic-link target-edit guardrails** — also 4.9b.
- **Auto-trade LIVE on IBKR or Tiger.** Each new broker's LIVE promotion needs its own Phase 4.6-style soak (OFF → DRY_RUN → LIVE-paper → 28-day LIVE-live), multiple months of calendar time per broker. 4.9a ships them as suggest-only readers/draft-submitters; LIVE promotion is a separate workstream.
- **Cross-broker order routing.** The system never moves cash, positions, or orders between brokers. The user picks the broker for each manual order.
- **Tax-lot-aware suggestions.** Phase 6+ at earliest.
- **Multi-currency targets.** IBKR and Tiger users may run accounts in non-USD; 4.9a converts to USD at snapshot time using the broker's reported FX or a single reference rate. Native multi-currency targets are out of scope.
- **Asset classes beyond US equities + ETFs.** Options, futures, crypto, fixed income exist in IBKR and Tiger but are explicitly ignored in 4.9a — `get_positions` filters to equities; non-equity holdings show as a single "Other" line with a footnote.

**Time budget:** 2–3 weeks. Two thirds of the work is in the IBKR and Tiger adapters + the data-model migration; the per-broker report wiring is mechanical once the partitioning is clean.

**Definition of done:** Jane connects a second broker (Moomoo paper or IBKR paper) on top of her existing Alpaca account and receives, on the next regular cron schedule: (a) two separate daily-report emails (subject line `[Alpaca] Daily report for YYYY-MM-DD` and `[Moomoo] Daily report for YYYY-MM-DD`); (b) two separate weekly-suggestions emails Sunday evening; and (c) every Phase 4.8 audit column (`base_qty`, `size_factor`, `context_note`) correctly populated per-broker for the new suggestions. Jane's existing single-broker data carries through migration with full audit history. The Phase 4.6 soak ladder's `alpaca_*` state is untouched; `moomoo` / `ibkr` / `tiger` ship as OFF. Tag: `v0.4.9a.0`.

**Depends on:**

- **Phase 4.8** (`v0.4.8.0`) tagged. The lifecycle bug fixes (B1–B6, G1–G3, structural stale-live-order guard) must be live before adding more broker adapters because each new adapter exercises every code path. Multi-broker against bug-prone reconciliation is worse than single-broker against bug-prone reconciliation.
- The Phase 4.7/4.8 cleanup-table items closed: `queries.py` / `sql/*.sql` duplication, flaky weekday-guard test, sug-23/AAPL pending resolution, CNN F&G `urlopen` timeout. None large; all worth closing before this phase's schema migrations land on top.

---

## Architecture context — what's new in Phase 4.9a

```
                       ┌──────────────────────────────────┐
                       │ user (1 row — Jane, for now)     │
                       │   (becomes auth.users in 5a)     │
                       └──────────────┬───────────────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        │                             │                             │
┌───────▼────────┐         ┌──────────▼─────┐           ┌───────────▼─────┐
│ broker_account │         │ broker_account │           │ broker_account  │
│  Alpaca paper  │         │   Moomoo       │           │   IBKR paper    │
│  nickname:     │         │   nickname:    │           │   nickname:     │
│  "Active US"   │         │   "Long-term"  │           │   "Tax shelter" │
└───────┬────────┘         └──────────┬─────┘           └───────────┬─────┘
        │                             │                             │
        │   each owns its own:        │                             │
        │   • target_allocation       │                             │
        │   • positions_snapshot      │                             │
        │   • order_suggestion        │                             │
        │   • order_execution         │                             │
        │   • auto_trade_state        │                             │
        │                             │                             │
        └────────────┬────────────────┴─────────────────────────────┘
                     │
              ┌──────▼──────────────────────────────┐
              │ user-scoped (broker-agnostic):       │
              │   • news_event                       │
              │   • weekly_market_context (Phase 4.7)│
              │   • sr_level (per-ticker;            │
              │     application to a draft           │
              │     is scoped per broker_account)    │
              │   • user_settings                    │
              └──────────────────────────────────────┘
```

Four behaviours to internalize:

1. **`broker_account_id` is the new partition key for everything that's per-account.** Positions, targets, suggestions, executions, and the auto-trade state are all per-broker. Adding a broker is adding rows, not a column. Removing a broker is soft-deleting the account row (`is_active = False`) — never hard-delete, never lose history.

2. **News, technical levels, and market context are user-level, not broker-level.** A news event about AAPL is a fact about AAPL; the user reads it once, regardless of how many brokers they hold AAPL in. `news_event`, `weekly_market_context`, `sr_level` (the underlying levels are per-ticker; the *application* to a specific draft is per-account) stay user-scoped. Don't duplicate them per broker — duplication breaks the audit story and burns LLM budget.

3. **Auto-trade LIVE is per-broker, with its own soak ladder per broker.** The Phase 4.6 OFF → DRY_RUN → LIVE-paper → LIVE-live progression now exists *per broker_account_id*. Adding IBKR doesn't auto-enable trading there; it just creates a new auto-trade-state row with `mode='OFF'`. Promoting IBKR to LIVE later is a deliberate per-broker soak, not a global flip.

4. **Suggest-only invariant holds across all brokers.** Adding three more brokers is adding three more surface areas for "could we add a quick auto-trade shortcut" requests. Resist. Every broker that gains LIVE auto-trade has to go through its own soak with its own hard caps and (in 5a+) its own attestation. Suggest-only is the default for any newly-connected broker, no exceptions.

---

## 0. Pre-flight checklist (~30 min)

- [ ] Phase 4.8 tagged (`v0.4.8.0`) with all cleanup-table items closed.
- [ ] Confirm `alembic upgrade head` is clean against Jane's current SQLite.
- [ ] Decide what `broker_account_id` for Jane's existing Alpaca data should be — recommend a stable UUID generated at migration time, stored alongside her account row (so it survives the Phase 5a SQLite → Postgres cut-over too).
- [ ] If you intend to test the IBKR adapter: install IB Gateway (or TWS) and create an IBKR paper account. The Gateway is a separate process — same host-side dependency model as Moomoo's OpenD.
- [ ] If you intend to test the Tiger adapter: create a Tiger Open API account (free at developer.tigerbrokers.com); generate the RSA key pair Tiger uses for request signing; note your account region (TBSG / TBAU / TBKR).

---

## 1. The `broker_account` table is now real (~half day)

Currently `broker_account` exists but is single-row (convention #9 close-and-insert pattern). For multi-broker, it becomes a real per-account table:

```python
class BrokerAccount(Base):
    __tablename__ = "broker_account"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # user_id added in Phase 5a; in 4.9 implicitly Jane
    broker: Mapped[str]                        # "alpaca_paper" | "alpaca_live" | "moomoo" | "ibkr_paper" | "ibkr_live" | "tiger"
    nickname: Mapped[str]                      # user-given, e.g., "Active US trading", "Long-term tax-shelter"
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
    # Time-versioned reference fields (convention #9 — close-and-insert when these change)
    cash_usd: Mapped[float]
    equity_usd: Mapped[float]
    effective_from: Mapped[datetime]
    effective_to:   Mapped[datetime | None]
    # Connection config (encrypted in Phase 5a; plaintext env-var refs in 4.9)
    connection_config: Mapped[str]             # JSON: API key references, OpenD host, IB Gateway host, Tiger region, etc.
    __table_args__ = (
        Index("ix_broker_account_active", "is_active"),
        Index("ix_broker_account_broker", "broker"),
    )
```

The `connection_config` column holds a JSON blob with broker-specific connection metadata (env var *names* in 4.9 single-user, then encrypted credentials in 5a). For Alpaca: `{"api_key_env": "ALPACA_API_KEY", "secret_env": "ALPACA_SECRET_KEY", "paper": true}`. For IBKR: `{"gateway_host": "host.docker.internal", "gateway_port": 4002, "client_id": 7}`. For Tiger: `{"tiger_id_env": "TIGER_ID_AU", "private_key_path": "...", "region": "TBAU"}`. Phase 5a moves credentials proper into the envelope-encrypted `user_broker_credentials` table; in 4.9a they stay in env vars and the config just names them.

`is_active` is soft-delete; never hard-delete a broker_account row, because all its historical positions/suggestions/executions point at it via FK.

---

## 2. Add `broker_account_id` to per-account tables (~1 day)

Alembic migration `phase4_9a_broker_account_id`:

```python
JANE_ALPACA_ID = "..."   # the UUID from step 1

# For each table:
op.add_column("target_allocation",  sa.Column("broker_account_id", sa.UUID(), nullable=True))
op.add_column("positions_snapshot", sa.Column("broker_account_id", sa.UUID(), nullable=True))
op.add_column("order_suggestion",   sa.Column("broker_account_id", sa.UUID(), nullable=True))
op.add_column("order_execution",    sa.Column("broker_account_id", sa.UUID(), nullable=True))
# 1. Backfill: all existing rows belong to Jane's Alpaca account
op.execute(f"UPDATE target_allocation  SET broker_account_id = '{JANE_ALPACA_ID}'")
op.execute(f"UPDATE positions_snapshot SET broker_account_id = '{JANE_ALPACA_ID}'")
op.execute(f"UPDATE order_suggestion   SET broker_account_id = '{JANE_ALPACA_ID}'")
op.execute(f"UPDATE order_execution    SET broker_account_id = '{JANE_ALPACA_ID}'")
# 2. NOT NULL
op.alter_column("target_allocation",  "broker_account_id", nullable=False)
# ... same for others ...
# 3. Composite indexes that previously keyed on (ticker,...) now key on (broker_account_id, ticker,...)
op.create_index("ix_target_alloc_account_ticker", "target_allocation", ["broker_account_id", "ticker"])
op.create_index("ix_positions_account_ticker_date", "positions_snapshot",
                ["broker_account_id", "ticker", "snapshot_date"])
op.create_index("ix_suggestion_account_week", "order_suggestion",
                ["broker_account_id", "week_of", "ticker", "side"], unique=True)
# Drop old unique constraints that didn't include broker_account_id
op.drop_constraint("uq_one_per_ticker_per_week", "order_suggestion", type_="unique")
```

> **Every existing `UniqueConstraint` must include `broker_account_id`.** Otherwise the Alpaca run on Sunday writes `(VOO, 2026-06-01, buy)` and the Moomoo run on Sunday conflicts. Same for `target_allocation.uq_one_per_ticker`. Get every existing unique constraint reviewed in this migration — drop the old, add the new with `broker_account_id` first.

Verify after migration: `SELECT broker_account_id, COUNT(*) FROM order_suggestion GROUP BY 1` returns exactly one row (Jane's Alpaca UUID) with the pre-migration row count. Any other shape is a bug in the backfill.

---

## 3. `auto_trade_state` table — per-broker auto-trade mode (~half day)

Previously `auto_trade_mode` lived as a single row in `meta`. Now it's per-broker:

```python
class AutoTradeState(Base):
    __tablename__ = "auto_trade_state"
    broker_account_id: Mapped[UUID] = mapped_column(ForeignKey("broker_account.id"), primary_key=True)
    mode: Mapped[str]                          # "OFF" | "DRY_RUN" | "LIVE"
    promoted_at: Mapped[datetime | None]
    promotion_soak_complete_at: Mapped[datetime | None]
    last_kill_switch_event: Mapped[datetime | None]
    # Per-broker hard caps (override settings.default_*)
    per_order_cap_usd: Mapped[float | None]
    per_day_cap_usd:   Mapped[float | None]
    per_week_per_ticker_cap_usd: Mapped[float | None]
    per_day_order_count_cap: Mapped[int | None]
```

Migration: for Jane's existing Alpaca account, copy `meta.auto_trade_mode` into the new `auto_trade_state` row; drop the old `meta` key. Every new broker_account row defaults to `mode='OFF'`.

`POST /admin/auto-trade/promote` now takes a required `broker_account_id` query param. ADR-0024 records the per-broker soak ladder semantics (every broker promotes independently; promoting Alpaca to LIVE does not promote Moomoo).

The Phase 4.8 structural `_check_stale_live_order` guard now scopes by `(broker_account_id, ticker)` — two live orders for the same ticker across *different* brokers is fine (that's the multi-broker model); two live orders for the same ticker on the *same* broker is still what the guard prevents.

---

## 4. New brokers — IBKR adapter (~1 week)

IBKR uses a persistent socket connection through IB Gateway (or TWS Desktop), not REST. The right SDK is `ib_insync` (community wrapper around `ibapi`, much friendlier ergonomics):

```python
# pyproject.toml
"ib-insync >= 0.9, < 0.10"
```

```python
# src/investor/brokers/ibkr.py
from ib_insync import IB, Stock, LimitOrder

class IBKRAdapter(BrokerAdapter):
    """IBKR via ib_insync against IB Gateway / TWS.

    Connection model: persistent socket; one IB() instance per adapter.
    Gateway runs on the host (host.docker.internal:4002 from Docker; localhost:4002 native).
    Client ID is per-process — pick a stable one per broker_account (clashes silently
    disconnect the other client).
    """

    def __init__(self, *, host: str, port: int, client_id: int, paper: bool):
        self._ib = IB()
        self._host, self._port, self._cid = host, port, client_id
        self._paper = paper

    def _connect(self) -> None:
        if not self._ib.isConnected():
            self._ib.connect(self._host, self._port, clientId=self._cid, timeout=10)

    def get_positions(self) -> list[Position]:
        self._connect()
        portfolio = self._ib.portfolio()                  # snapshot of current positions
        return [
            Position(ticker=p.contract.symbol,
                     qty=float(p.position),
                     market_value=float(p.marketValue),
                     avg_cost=float(p.averageCost))
            for p in portfolio
            if p.contract.secType == "STK"                # equities only; ignore options/futures
        ]

    def get_account(self) -> AccountSnapshot:
        self._connect()
        summary = self._ib.accountSummary()
        # Tags of interest: NetLiquidation, TotalCashValue, BuyingPower
        # IBKR may return in account base currency; convert if needed (out of scope for 4.9a — assume USD account)
        ...

    def submit_order_draft(self, req: OrderDraft) -> OrderConfirmation:
        # Phase 4.6 unchanged — drafts only return; nothing routed
        ...

    def submit_order(self, req: OrderRequest) -> OrderConfirmation:
        # Phase 4.6 LIVE — OFF by default for IBKR in 4.9a
        # When eventually enabled, use LimitOrder(action, qty, lmtPrice=_floor2dp(req.limit_price), tif="GTC")
        ...

    def cancel_order(self, broker_order_id: str) -> None: ...
    def get_order(self, broker_order_id: str) -> OrderConfirmation: ...
    def list_orders(self, status: str) -> list[OrderConfirmation]: ...
```

Three IBKR-specific gotchas worth knowing:

- **Gateway has a daily auto-restart at 23:45 ET.** Adapter must reconnect on `EOFError` / `ConnectionResetError`. Don't crash the cron; reconnect-and-retry-once is the right pattern.
- **`accountSummary` returns strings.** Wrap every numeric in `float()` at the adapter boundary, same lesson as Alpaca.
- **`client_id` must be unique per active connection.** If you have TWS open with `client_id=7` and the cron also connects with the same `client_id`, one drops. Pick a stable, documented client_id per broker_account (store it in `connection_config`); the GUI/manual-checks client_id should be different.

ADR-0025 records the design choice (`ib_insync` over raw `ibapi`; Gateway as host-side dependency; reconnect-on-error pattern).

---

## 5. New brokers — Tiger adapter (~3–5 days)

Tiger uses REST + signed requests:

```python
# pyproject.toml
"tigeropen >= 3.0, < 4.0"
```

```python
# src/investor/brokers/tiger.py
from tigeropen.common.consts import Language, Market
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.quote.quote_client import QuoteClient
from tigeropen.trade.trade_client import TradeClient

class TigerAdapter(BrokerAdapter):
    """Tiger Brokers via tigeropen SDK.

    Auth: Tiger ID + RSA private key (per-account).
    Regions: TBSG (Singapore), TBAU (Australia), TBKR (HK), TBHK.
    """

    def __init__(self, *, tiger_id: str, private_key_path: str, region: str, account: str):
        cfg = TigerOpenClientConfig()
        cfg.tiger_id = tiger_id
        cfg.private_key = read_private_key(private_key_path)
        cfg.account = account                              # paper or live account number
        cfg.language = Language.en_US
        self._trade = TradeClient(cfg)
        self._quote = QuoteClient(cfg)
        self._region = region
        self._account = account

    def get_positions(self) -> list[Position]:
        positions = self._trade.get_positions(account=self._account, sec_type="STK")
        return [
            Position(ticker=p.contract.symbol,
                     qty=float(p.quantity),
                     market_value=float(p.market_value),
                     avg_cost=float(p.average_cost))
            for p in positions
        ]

    def get_account(self) -> AccountSnapshot: ...
    def submit_order_draft(self, req: OrderDraft) -> OrderConfirmation: ...
    def submit_order(self, req: OrderRequest) -> OrderConfirmation: ...
    def cancel_order(self, broker_order_id: str) -> None: ...
    def get_order(self, broker_order_id: str) -> OrderConfirmation: ...
    def list_orders(self, status: str) -> list[OrderConfirmation]: ...
```

Tiger-specific gotchas:

- **Non-USD account currency.** TBAU accounts default to AUD. Tiger reports `cash` and `equity` in the account currency; the adapter must convert to USD using `quote.get_market_state()` for the FX rate or a single reference rate. For 4.9a, store both: `cash_native`, `currency`, `cash_usd_at_snapshot`. The consolidated view in 4.9b uses `cash_usd_at_snapshot`.
- **RSA key rotation.** Tiger's private key has a renewal cycle; treat it the same way as Alpaca API key rotation (alert when expiry < 30 days; store in encrypted creds in 5a).
- **Rate limits.** Tiger's free tier is 60 req/min like Finnhub. Cache `get_account()` for 5 minutes during the daily-report cron to avoid bursting.

ADR-0026 records the adapter design and the FX-at-snapshot-time convention.

---

## 6. Broker factory and connection lifecycle (~half day)

`BrokerAdapter` is the only door (convention #1). The factory now takes a `BrokerAccount` row and returns the right concrete adapter:

```python
# src/investor/brokers/__init__.py
def make_adapter(account: BrokerAccount) -> BrokerAdapter:
    cfg = json.loads(account.connection_config)
    match account.broker:
        case "alpaca_paper":
            return AlpacaAdapter(api_key=os.environ[cfg["api_key_env"]],
                                 secret=os.environ[cfg["secret_env"]],
                                 paper=True)
        case "alpaca_live":
            return AlpacaAdapter(api_key=os.environ[cfg["api_key_env"]],
                                 secret=os.environ[cfg["secret_env"]],
                                 paper=False)
        case "moomoo":
            return MoomooAdapter(host=cfg["opend_host"], port=cfg["opend_port"])
        case "ibkr_paper" | "ibkr_live":
            return IBKRAdapter(host=cfg["gateway_host"], port=cfg["gateway_port"],
                               client_id=cfg["client_id"], paper=account.broker == "ibkr_paper")
        case "tiger":
            return TigerAdapter(tiger_id=os.environ[cfg["tiger_id_env"]],
                                private_key_path=cfg["private_key_path"],
                                region=cfg["region"], account=cfg["tiger_account"])
        case _:
            raise ValueError(f"unsupported broker: {account.broker}")
```

The lifespan creates one adapter per active broker_account at startup and stores them on `app.state.adapters: dict[UUID, BrokerAdapter]`. Crons that operate per-broker (daily report, weekly suggestions, reconciliation) iterate `app.state.adapters.items()`.

IBKR's persistent connection complicates lifecycle: the adapter has to handle Gateway disconnects (daily restart at 23:45 ET), and per-process socket exhaustion if the loop reconstructs adapters frequently. Recommended: one adapter per broker_account at process boot, with a reconnect-on-error wrapper that wraps every call.

---

## 7. Reports per broker (~3–4 days)

### 7a. Daily report

`jobs/daily_report.py` becomes a loop over active broker accounts:

```python
def run_daily_report_all_brokers(settings, emailer):
    with session_scope() as s:
        accounts = s.scalars(select(BrokerAccount).where(BrokerAccount.is_active == True)).all()
    for acct in accounts:
        try:
            run_daily_report_for_account(acct, settings, emailer)
        except Exception:
            log.exception("daily report failed for broker_account=%s (%s); continuing",
                          acct.id, acct.nickname)
            continue
```

`run_daily_report_for_account` is the existing daily-report function with `broker_account_id=acct.id` threaded through every query. The email goes out per account with subject `[{nickname}] Daily report for YYYY-MM-DD`.

A new `user_settings.email_aggregation` field is added (4.9a value: `per_broker` only; the `consolidated` and `both` options are wired in 4.9b once household summary exists).

### 7b. Weekly suggestions

`jobs/weekly_suggestions.py` becomes a loop over active broker accounts, each running its own suggestion-review graph:

```python
for acct in active_accounts:
    drafts = generate_suggestions(broker_account_id=acct.id, ...)
    graph = build_suggestion_review_graph(llm, session_scope,
                                          settings=settings, earnings_client=earnings_client)
    result = graph.invoke({"week_of": next_monday(),
                           "broker_account_id": acct.id,
                           "drafts": drafts, ...})
```

`ReviewContext` gains `broker_account_id` and `account_snapshot` is scoped to that broker. The Phase 4.7 `context_adjust_node` reads the *user-level* `weekly_market_context` (one synthesis serves all brokers — news is news) but scales drafts within each broker's per-account context. The Phase 3c critic reasons across a broker's draft set (cross-suggestion cash floor, concentration) per-broker — never across brokers.

### 7c. Email template per-broker

`templates/daily_report.html.j2` gains a single new top-of-email line: `<small>{{ account.nickname }} ({{ account.broker }})</small>` — every existing section stays. Subject line gets the `[{nickname}]` prefix.

`templates/weekly_suggestions.html.j2` mirror.

> **Sundays in 4.9a generate N weekly emails.** With 4 brokers, that's 4 weekly emails Sunday evening — the user might wish they'd configured `consolidated`, but that mode only exists after 4.9b ships. Document this in the onboarding-to-second-broker flow: "you'll now receive a separate email per broker; you can switch to consolidated after Phase 4.9b lands."

---

## 8. `targets.yaml` → per-broker (~2 days)

`targets.yaml` is single-source. With multi-broker, it splits per account:

```
data/targets/
  <broker_account_id_1>.yaml    # Jane's Alpaca targets
  <broker_account_id_2>.yaml    # Jane's Moomoo targets
  <broker_account_id_3>.yaml    # Jane's IBKR targets
```

`load_targets_into_db()` gains a `broker_account_id` parameter and reads the per-account file. The `target_allocation` rows it inserts carry the `broker_account_id`. Existing `target_allocation` rows are backfilled in step 2 to Jane's Alpaca account.

Phase 5a continues this work — replacing the YAML files with DB rows authored via the dashboard. In 4.9a the YAML file path remains for editing convenience.

> Phase 4.9b adds the magic-link confirmation guardrail on YAML edits; in 4.9a the edit path is unguarded. That's fine if you're the only editor and only adding/renaming files (small risk window); just be aware that 4.9b is what closes it.

---

## 9. Per-broker reconciliation & expiry sweep (~1 day)

`services/reconciliation.py` becomes per-broker: `reconcile_activities(broker_account_id, adapter, session)`. The Phase 4.8 `sync_open_order_statuses` batch `list_orders` call happens per broker (each broker has its own list of open orders).

`jobs/suggestion_expiry.py` loops over brokers; for each, queries expired-but-still-active suggestions for that broker and cancels via the corresponding adapter.

---

## 10. Smoke-test checklist (Phase 4.9a done when all green)

| # | Step | Pass criteria |
|---|---|---|
| 1 | Migration: Jane's existing data carries through; every `target_allocation`, `positions_snapshot`, `order_suggestion`, `order_execution` row has `broker_account_id` = Jane's Alpaca UUID; no orphaned rows | ✓ |
| 2 | Add a second `BrokerAccount` row (Moomoo paper); `make_adapter` returns the right concrete adapter; `get_positions()` returns the Moomoo positions | ✓ |
| 3 | Daily report fires per active broker; receives two emails on a 2-broker setup with `[{nickname}] Daily report ...` subjects | ✓ |
| 4 | Weekly suggestions Sunday: each broker gets its own suggestions email; cross-broker tickers (e.g., VOO held in both Alpaca and Moomoo) generate independent suggestions per broker | ✓ |
| 5 | Per-broker `auto_trade_state`: promoting Alpaca to LIVE does not affect Moomoo (still OFF) | ✓ |
| 6 | Phase 4.8 stale-live-order guard scope: two live orders for AAPL across different brokers is allowed; two on the same broker still blocked | ✓ |
| 7 | IBKR Gateway reconnect: kill the Gateway process during a cron run; adapter logs a clear reconnect attempt; cron completes within retry budget | ✓ |
| 8 | Tiger FX: TBAU paper account in AUD reports `cash_native=N AUD` and `cash_usd_at_snapshot=N*fx`; email shows USD value with footnote `(N AUD × 0.66)` | ✓ |
| 9 | `targets/<broker_account_id>.yaml` files edited independently; targets load per broker; old single-file `targets.yaml` path is removed (or aliased to Jane's primary account for one release) | ✓ |
| 10 | Soft-delete a broker_account: `is_active=False` excludes it from cron loops; historical positions/suggestions for that account remain queryable | ✓ |
| 11 | `client_id` collision test: a deliberately-conflicting second IBKR adapter raises a clear "client_id already in use" error rather than silently displacing the first | ✓ |
| — | `uv run pytest` overall | New broker tests pass; total ≥ 360 (342 at 4.8 close + new); `ruff` and `mypy` clean |

Tag:

```bash
git tag v0.4.9a.0
git push --tags
```

---

## Common Phase 4.9a pitfalls

1. **Backfilling `broker_account_id` is one shot.** Step 2's migration writes all existing per-account rows to Jane's Alpaca UUID. If you get the UUID wrong (typo, copy-paste error), the unique constraints will look correct but every cross-broker query will return Jane's data only. Verify with `SELECT broker_account_id, COUNT(*) FROM order_suggestion GROUP BY 1` after migration; one row, one count, equal to pre-migration total.
2. **IBKR Gateway daily restart at 23:45 ET.** If your overnight bar-sync job runs across that boundary, the IBKR connection will silently drop mid-job. Either schedule jobs to avoid 23:30–00:30 ET or implement reconnect-on-error at every adapter call. The Moomoo OpenD has similar issues — both are host-side dependencies, not API services.
3. **Tiger non-USD accounts surprise the daily report.** TBAU account holds AUD 50,000 in cash; that's AUD or USD in the email? Always USD via `cash_usd_at_snapshot`, with a footnote showing the FX. Smoke row 8 enforces.
4. **`client_id` collision on IBKR.** If you connect with `client_id=7` from the cron and you also have TWS Desktop running with `client_id=7`, one of them silently disconnects. Pick stable, unique client_ids per active context and document in `connection_config`. Smoke row 11 catches this explicitly.
5. **Cross-broker duplicate suggestions are correct, not a bug.** A user with VOO in both Alpaca and Moomoo, both under-target per their per-broker targets, gets two "buy VOO" suggestions Sunday — one per broker. The user's allocation is per-broker; don't try to dedup across brokers in 4.9a. The household drift table in 4.9b is the place to surface "you're over-allocated to VOO at the household level."
6. **Per-broker reports flood the inbox.** With 4 brokers, Sunday evening sends 4 weekly-suggestions emails + 4 daily emails Monday morning + the Phase 4.5 weekly review Friday. That's 9 emails/week before 4.9b's consolidated option is configured. Tell the user during multi-broker onboarding: "expect N emails/day until 4.9b ships."
7. **IBKR + Moomoo Gateways are *two* separate host-side dependencies.** Docker Compose's `extra_hosts: ["host.docker.internal:host-gateway"]` only solves the network path; you still need IB Gateway running *and* OpenD running for both adapters to work. Document the host-side install requirements once and link to it from CLAUDE.md — otherwise a fresh dev environment takes hours to get past adapter init errors.
8. **Unique constraints are easy to forget on Alembic auto-generate.** Alembic's autogenerate misses subtle changes to composite unique constraints when only one column changes. Hand-edit the generated migration after running `alembic revision --autogenerate` and re-check every `UniqueConstraint` mentioned in step 2. Don't trust autogenerate for this migration.

---

## ADRs to write in Phase 4.9a

- **ADR-0024 — Multi-broker single-user data model.** Why `broker_account_id` on every per-account table (alternative considered: a separate per-broker schema; rejected for join complexity). Why news/levels/market-context stay user-level. The unique-constraint conversion rule. The soft-delete-only policy for broker_account. The per-broker auto-trade soak-ladder semantics.
- **ADR-0025 — IBKR adapter via `ib_insync` against IB Gateway.** Alternatives considered: raw `ibapi` (too low-level), IBKR Web API (limited surface, no GTC). Acknowledges the host-side dependency same as Moomoo. The reconnect-on-error pattern; the `client_id` uniqueness convention.
- **ADR-0026 — Tiger adapter via `tigeropen`; FX conversion at snapshot time.** Tiger's regional account types; conversion to USD at the adapter boundary; the dual `cash_native` / `cash_usd_at_snapshot` storage pattern.

The Phase 5 guide currently allocates ADRs 0024–0031. Phase 4.9 ships first, so 4.9a takes 0024–0026, 4.9b will take 0027–0029, and Phase 5 ADRs shift to 0030–0037. Update the Phase 5 guide before its first commit.

---

## Documentation drift to fix

- **CLAUDE.md:**
  - **Mission paragraph:** "pulls positions from a broker (Alpaca first, Moomoo later)" → "pulls positions from one or more brokers per user — Alpaca, Moomoo, IBKR, Tiger as of Phase 4.9a."
  - **Convention #3:** "Domain IDs ≠ broker IDs. Tables key on `(ticker)` or `(user_id, ticker)`" → "Tables key on `(broker_account_id, ticker)` or `(user_id, ticker)` depending on whether the data is per-account or user-level."
  - **Convention #11 (auto-trade):** "The `auto_trade_mode` setting (stored in `meta` table…)" → "stored in `auto_trade_state` per broker_account_id; promotion is per-broker, not global. The Phase 4.6 soak ladder applies independently per broker."
  - **Repo layout:** add `brokers/ibkr.py`, `brokers/tiger.py`; new daily-report and weekly-suggestion loop wiring.
  - **Common gotchas:** add "IBKR Gateway daily restart"; "Tiger regional account types and FX conversion at snapshot time"; "`client_id` collision on IBKR".
  - **Required env vars:** new `IBKR_GATEWAY_HOST`, `IBKR_GATEWAY_PORT`, `IBKR_CLIENT_ID_*` per account; `TIGER_ID_*`, `TIGER_PRIVATE_KEY_PATH_*`, `TIGER_REGION_*` per account.
- **`product_plan.md`:** add a Phase 4.9a entry between 4.8 and Phase 5. The Phase 5 entry's "Depends on" reference Phase 4.9 (a and b).
- **`phase_5_guide.md`:** the "Phase 4.9 prerequisite" section currently describes rebalance reviews only; expand to mention 4.9a multi-broker as the bigger first half. Phase 5a's "user_id on every row" migration is now simpler because `broker_account_id` already exists — note the simplification.
- **`README.md`:** "What brokers are supported" section gains IBKR and Tiger with the same caveats (suggest-only at v1; LIVE requires per-broker soak progression).
- **ADR index:** add 0024–0026.

---

## What Phase 4.9a deliberately does not include

- **Household target allocation and the consolidated summary email.** Phase 4.9b.
- **Funds-added detection, quarterly/annual review crons, magic-link target-edit guardrails.** Phase 4.9b.
- **IBKR auto-trade LIVE.** Each new broker needs its own Phase 4.6 OFF → DRY_RUN → LIVE soak. That's calendar time, not engineering time — done after 4.9 ships and the broker's read paths have been observed for a few weeks.
- **Tiger auto-trade LIVE.** Same reasoning. Plus Tiger's order semantics (regional differences, currency conversion at order time) deserve their own thinking.
- **Cross-broker order routing.** The product is suggest-only at the household level; the user picks the broker for each manual order.
- **Lot-level cost basis tracking.** Phase 6+ at earliest.
- **Native multi-currency target support.** Non-USD brokers convert to USD at snapshot time.
- **Schwab, Fidelity, Robinhood, Webull, Trading 212 adapters.** Wait for user demand before adding a fifth broker.

---

*When all 11 4.9a smoke rows are green, Jane has connected at least two brokers and received separate emails per broker (per-broker daily + per-broker weekly suggestions), and the migration has produced no orphan rows in any audit query, Phase 4.9a is done. Tag `v0.4.9a.0`. Phase 4.9b layers the household target and consolidated summary on top.*
