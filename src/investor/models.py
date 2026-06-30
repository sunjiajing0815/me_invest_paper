"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Double,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime as _SADateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """DateTime that always returns tz-aware UTC datetimes from SQLite.
    SQLite stores without timezone; this re-attaches UTC on read."""

    impl = _SADateTime(timezone=True)
    cache_ok = True

    def process_result_value(
        self, value: datetime | None, dialect: object
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class Base(DeclarativeBase):
    pass


class TargetAllocation(Base):
    """Time-versioned target allocation rows. Never UPDATE in place; close and insert."""

    __tablename__ = "target_allocation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Partition key (= BrokerAccount.account_ref). Nullable during the 4.9a build;
    # backfilled + tightened to NOT NULL by migration. No DB FK (app-enforced).
    broker_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    target_pct: Mapped[float] = mapped_column(Double, nullable=False)
    band_low_pct: Mapped[float] = mapped_column(Double, nullable=False)
    band_high_pct: Mapped[float] = mapped_column(Double, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, default=None
    )
    __table_args__ = (
        Index("ix_target_alloc_account_ticker", "broker_account_id", "ticker"),
    )


class TargetChangeEvent(Base):
    """Append-only audit of every applied target-allocation edit (P2.1)."""

    __tablename__ = "target_change_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # = BrokerAccount.account_ref; NULL reserved for a future household-level edit (P2.4).
    broker_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )
    source: Mapped[str] = mapped_column(String, nullable=False)  # admin_endpoint|yaml_direct
    diff_json: Mapped[str] = mapped_column(Text, nullable=False)  # {"old": {t: pct}, "new": {...}}
    max_shift_pp: Mapped[float] = mapped_column(Double, nullable=False)  # largest |shift|
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )


class FundsEvent(Base):
    """Append-only record of a detected external cash flow (deposit/withdrawal) — P2.3."""

    __tablename__ = "funds_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )
    delta_usd: Mapped[float] = mapped_column(Double, nullable=False)  # signed external flow
    kind: Mapped[str] = mapped_column(String, nullable=False)  # deposit | withdrawal
    prev_cash: Mapped[float] = mapped_column(Double, nullable=False)
    cur_cash: Mapped[float] = mapped_column(Double, nullable=False)
    trade_cash_flow: Mapped[float] = mapped_column(Double, nullable=False)  # sells − buys
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )


class PositionsSnapshot(Base):
    """One row per ticker per sync. Weight is computed at write time."""

    __tablename__ = "positions_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Partition key (= BrokerAccount.account_ref). Distinct from account_id, which
    # is the broker's own account identifier string. No DB FK (app-enforced).
    broker_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[float] = mapped_column(Double, nullable=False)
    avg_cost: Mapped[float] = mapped_column(Double, nullable=False)
    market_value: Mapped[float] = mapped_column(Double, nullable=False)
    weight_pct: Mapped[float] = mapped_column(Double, nullable=False)
    # Native currency of price/market_value (USD for US, AUD for ASX, …). The account
    # summary totals are in the account's base currency; per-position values stay native.
    currency: Mapped[str] = mapped_column(String, nullable=False, server_default="USD")
    __table_args__ = (
        Index("ix_positions_account_ticker_ts", "broker_account_id", "ticker", "ts"),
    )


class Meta(Base):
    """Key/value store for app-level metadata (e.g. YAML content hashes)."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class BrokerAccount(Base):
    """Dual-purpose: broker-account identity + time-versioned account state.

    Phase 4.9a (multi-broker): this table holds both the stable identity of a
    broker account (``account_ref``, ``nickname``, ``is_active``,
    ``connection_config``) and its close-and-insert cash/equity state history.
    ``account_ref`` is the stable partition key — constant across an account's
    state rows — that every per-account table references via ``broker_account_id``.
    The auto-increment ``id`` changes on each state insert and must NOT be used as
    a foreign key. The latest open row (``effective_to IS NULL``) is the single
    source of truth for both identity and current state; ``snapshot.py`` carries
    the identity columns forward when it close-and-inserts a new state row.
    """

    __tablename__ = "broker_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Stable partition key (constant across this account's state rows). Backfilled
    # by the phase4_9a migration; tightened to NOT NULL once all writers set it.
    account_ref: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    broker: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)  # broker mode: "paper" | "live"
    # Identity columns (carried forward on each close-and-insert state row).
    nickname: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    connection_config: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON blob
    cash_usd: Mapped[float] = mapped_column(Double, nullable=False)
    equity_usd: Mapped[float] = mapped_column(Double, nullable=False)
    last_sync: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, default=None
    )
    __table_args__ = (Index("ix_broker_account_ref", "account_ref"),)


class SRLevel(Base):
    """Support/resistance level computed for a ticker on a specific date."""

    __tablename__ = "sr_level"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)    # "support" | "resistance"
    price: Mapped[float] = mapped_column(Double, nullable=False)
    method: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "pivot_weekly_S1", "sma_50"
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    llm_rationale: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    scored_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, default=None
    )
    scored_by_model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    __table_args__ = (
        UniqueConstraint("ticker", "method", "as_of", name="uq_sr_per_method_per_day"),
    )


class OrderSuggestion(Base):
    """Weekly suggestion. Status tracks accept/reject lifecycle."""

    __tablename__ = "order_suggestion"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Partition key (= BrokerAccount.account_ref). No DB FK (app-enforced).
    broker_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    week_of: Mapped[date] = mapped_column(Date, nullable=False)         # Monday of the week
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)           # "buy" | "sell"
    qty: Mapped[float] = mapped_column(Double, nullable=False)
    limit_price: Mapped[float] = mapped_column(Double, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    target_allocation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    confidence_at_creation: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    anchor_method: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    acted_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, default=None
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    base_qty: Mapped[float | None] = mapped_column(Double, nullable=True)
    size_factor: Mapped[float] = mapped_column(
        Double, nullable=False, default=1.0, server_default="1.0"
    )
    context_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "broker_account_id", "week_of", "ticker", "side",
            name="uq_suggestion_account_week",
        ),
    )


class LLMCallLog(Base):
    """Audit log for every Anthropic API call made by the app."""

    __tablename__ = "llm_call_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )
    purpose: Mapped[str] = mapped_column(String, nullable=False)       # e.g. "score_levels"
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Double, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # "ok" | "schema_error" | "api_error"
    status: Mapped[str] = mapped_column(String, nullable=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # Sampling temperature applied (NULL = backend couldn't set it, e.g. agent_sdk).
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    # Prompt-cache tiers — billed at 1.25x (write) / 0.10x (read) of base input. Stored
    # separately so cost is tier-correct and the cache-hit ratio is derivable; 0 when
    # caching is inactive or unsupported (agent_sdk).
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class NewsEvent(Base):
    """News article fetched from Alpaca or Finnhub, optionally scored by LLM."""

    __tablename__ = "news_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)       # "alpaca" | "finnhub"
    headline: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    url_hash: Mapped[str] = mapped_column(String, nullable=False)     # sha256(normalised_url)[:16]
    llm_material: Mapped[bool | None] = mapped_column(nullable=True, default=None)
    llm_sentiment: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    llm_summary: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    llm_model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    llm_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    arbitrated: Mapped[bool] = mapped_column(nullable=False, default=False)
    __table_args__ = (UniqueConstraint("url_hash", name="uq_news_url_hash"),)


class MoverState(Base):
    """Per-ticker threshold state for the tiered intraday-mover alert logic."""

    __tablename__ = "mover_state"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    last_triggered_threshold: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    # 0.0 = never triggered (or reset); 5.0 = 5% threshold last triggered; increments by 5.0
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, default=None
    )
    last_pct_change: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)


class OrderExecution(Base):
    """Filled (or dry-run) order record, linked optionally to an OrderSuggestion."""

    __tablename__ = "order_execution"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Partition key (= BrokerAccount.account_ref). No DB FK (app-enforced).
    broker_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # nullable — manual trades have no suggestion
    suggestion_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # No FK constraint — codebase-wide convention; app layer enforces referential integrity
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)  # "buy" | "sell"
    submitted_qty: Mapped[float | None] = mapped_column(Double, nullable=True)
    filled_qty: Mapped[float] = mapped_column(Double, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Double, nullable=True)
    filled_price: Mapped[float | None] = mapped_column(Double, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    broker: Mapped[str] = mapped_column(String, nullable=False)  # "alpaca" | "moomoo"
    # NULL for DRY_RUN rows
    broker_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # "sug-N" for auto-trade
    client_order_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # default TRUE (safe)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    # filled|partially_filled|rejected|expired|accepted_for_routing|dry_run
    status: Mapped[str] = mapped_column(String, nullable=False)
    realized_pnl_usd: Mapped[float | None] = mapped_column(Double, nullable=True)  # sells only
    # auto_trade_placed|auto_matched|manual_review|untracked
    match_method: Mapped[str] = mapped_column(String, nullable=False)
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )
    # When status was flipped to 'broker_cancelled' (P1.3) — drives manual-cancel inference.
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (
        UniqueConstraint("broker_order_id", "broker", name="uq_broker_order_id"),
        Index("ix_order_execution_ticker_filled_at", "ticker", "filled_at"),
    )


class AutoTradePromotionLog(Base):
    """Audit log for every auto-trade mode promotion or demotion event."""

    __tablename__ = "auto_trade_promotion_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    from_mode: Mapped[str] = mapped_column(String, nullable=False)
    to_mode: Mapped[str] = mapped_column(String, nullable=False)
    # alpaca_paper|alpaca_live|moomoo
    broker_scope: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "admin" | "kill_switch" | "guard_failure"
    actor: Mapped[str] = mapped_column(String, nullable=False)


class KillSwitchLog(Base):
    """Audit log for every kill-switch activation."""

    __tablename__ = "kill_switch_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # manual|cap_breach|readback_mismatch|broker_error
    trigger: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_order_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)


class AutoTradeCaps(Base):
    """Time-versioned spending/order caps for the auto-trade engine."""

    __tablename__ = "auto_trade_caps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    per_order_max_usd: Mapped[float] = mapped_column(Double, nullable=False)
    per_day_max_usd: Mapped[float] = mapped_column(Double, nullable=False)
    per_week_max_usd_per_ticker: Mapped[float] = mapped_column(Double, nullable=False)
    per_day_max_orders: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class AutoTradeState(Base):
    """Per-broker-account auto-trade mode + optional cap overrides.

    Phase 4.9a replaces the single ``meta.auto_trade_mode`` key with one row per
    broker account (keyed by ``BrokerAccount.account_ref``). Each broker promotes
    through its own OFF → DRY_RUN → LIVE soak ladder independently — promoting
    Alpaca does not promote Moomoo. Cap overrides are nullable; when NULL the
    engine falls back to the global time-versioned ``auto_trade_caps`` row.
    """

    __tablename__ = "auto_trade_state"

    broker_account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(
        String, nullable=False, default="OFF", server_default="OFF"
    )  # "OFF" | "DRY_RUN" | "LIVE"
    promoted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    promotion_soak_complete_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    last_kill_switch_event: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    per_order_cap_usd: Mapped[float | None] = mapped_column(Double, nullable=True)
    per_day_cap_usd: Mapped[float | None] = mapped_column(Double, nullable=True)
    per_week_per_ticker_cap_usd: Mapped[float | None] = mapped_column(Double, nullable=True)
    per_day_order_count_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WeeklyMarketContextRow(Base):
    """Persisted weekly market context produced by the Friday review job."""

    __tablename__ = "weekly_market_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_of: Mapped[date] = mapped_column(Date, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=lambda: datetime.now(UTC)
    )
    __table_args__ = (Index("ix_wmc_week_of", "week_of"),)  # NOT unique — re-runs allowed
