"""SQLAlchemy ORM models for Phase 0 tables.

IMPORTANT: Every primary key uses Integer + Sequence to avoid DuckDB's
lack of SERIAL/BIGSERIAL support.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Double, Integer, Sequence, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TargetAllocation(Base):
    """Time-versioned target allocation rows. Never UPDATE in place; close and insert."""

    __tablename__ = "target_allocation"

    id: Mapped[int] = mapped_column(
        Integer, Sequence("target_allocation_id_seq"), primary_key=True
    )
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    target_pct: Mapped[float] = mapped_column(Double, nullable=False)
    band_low_pct: Mapped[float] = mapped_column(Double, nullable=False)
    band_high_pct: Mapped[float] = mapped_column(Double, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


class PositionsSnapshot(Base):
    """One row per ticker per sync. Weight is computed at write time."""

    __tablename__ = "positions_snapshot"

    id: Mapped[int] = mapped_column(
        Integer, Sequence("positions_snapshot_id_seq"), primary_key=True
    )
    account_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
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

    id: Mapped[int] = mapped_column(
        Integer, Sequence("broker_account_id_seq"), primary_key=True
    )
    account_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    broker: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    cash_usd: Mapped[float] = mapped_column(Double, nullable=False)
    equity_usd: Mapped[float] = mapped_column(Double, nullable=False)
    last_sync: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    effective_to: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
