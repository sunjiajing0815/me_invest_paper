"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Double, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TargetAllocation(Base):
    """Time-versioned target allocation rows. Never UPDATE in place; close and insert."""

    __tablename__ = "target_allocation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    target_pct: Mapped[float] = mapped_column(Double, nullable=False)
    band_low_pct: Mapped[float] = mapped_column(Double, nullable=False)
    band_high_pct: Mapped[float] = mapped_column(Double, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


class PositionsSnapshot(Base):
    """One row per ticker per sync. Weight is computed at write time."""

    __tablename__ = "positions_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[float] = mapped_column(Double, nullable=False)
    avg_cost: Mapped[float] = mapped_column(Double, nullable=False)
    market_value: Mapped[float] = mapped_column(Double, nullable=False)
    weight_pct: Mapped[float] = mapped_column(Double, nullable=False)


class Meta(Base):
    """Key/value store for app-level metadata (e.g. YAML content hashes)."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class BrokerAccount(Base):
    """Time-versioned account state. Deduplicates unchanged values on write."""

    __tablename__ = "broker_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    broker: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    cash_usd: Mapped[float] = mapped_column(Double, nullable=False)
    equity_usd: Mapped[float] = mapped_column(Double, nullable=False)
    last_sync: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


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
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    llm_rationale: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    scored_by_model: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    __table_args__ = (
        UniqueConstraint("ticker", "method", "as_of", name="uq_sr_per_method_per_day"),
    )


class OrderSuggestion(Base):
    """Weekly suggestion. Status tracks accept/reject lifecycle."""

    __tablename__ = "order_suggestion"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_of: Mapped[date] = mapped_column(Date, nullable=False)         # Monday of the week
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)           # "buy" | "sell"
    qty: Mapped[float] = mapped_column(Double, nullable=False)
    limit_price: Mapped[float] = mapped_column(Double, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    target_allocation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence_at_creation: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    note: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    __table_args__ = (
        UniqueConstraint("week_of", "ticker", "side", name="uq_one_per_ticker_per_week"),
    )


class LLMCallLog(Base):
    """Audit log for every Anthropic API call made by the app."""

    __tablename__ = "llm_call_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
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
