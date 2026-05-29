"""adopt legacy create_all tables

Brings the three tables that were historically created only by SQLAlchemy
``create_all`` — ``broker_account``, ``target_allocation``, ``positions_snapshot``
— under Alembic ownership, so the migration chain builds the complete schema on
its own. Without this, a fresh ``alembic upgrade head`` (or a brand-new
``init_db`` deploy) failed: those tables never existed, and later migrations
that touch them (notably ``d8589`` adding ``broker_account_id``) errored.

Each CREATE is inspector-guarded, so on Jane's existing DB (where the tables
already exist via ``create_all``) this migration is a no-op. The tables are
created in their PRE-4.9a shape; the immediately-following ``d8589`` migration
adds the multi-broker columns. Chained between ``62b0733b198f`` and ``d8589``.

Revision ID: 7d25844a8a9a
Revises: 62b0733b198f
Create Date: 2026-05-29 19:56:58.810396

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7d25844a8a9a"
down_revision: str | Sequence[str] | None = "62b0733b198f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the legacy create_all-only tables if they are absent (pre-4.9a shape)."""
    insp = sa.inspect(op.get_bind())

    if not insp.has_table("broker_account"):
        op.create_table(
            "broker_account",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.String(), nullable=True),
            sa.Column("broker", sa.String(), nullable=False),
            sa.Column("mode", sa.String(), nullable=False),
            sa.Column("cash_usd", sa.Double(), nullable=False),
            sa.Column("equity_usd", sa.Double(), nullable=False),
            sa.Column("last_sync", sa.DateTime(timezone=True), nullable=False),
            sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
            sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    if not insp.has_table("target_allocation"):
        op.create_table(
            "target_allocation",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ticker", sa.String(), nullable=False),
            sa.Column("target_pct", sa.Double(), nullable=False),
            sa.Column("band_low_pct", sa.Double(), nullable=False),
            sa.Column("band_high_pct", sa.Double(), nullable=False),
            sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    if not insp.has_table("positions_snapshot"):
        op.create_table(
            "positions_snapshot",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.String(), nullable=True),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ticker", sa.String(), nullable=False),
            sa.Column("qty", sa.Double(), nullable=False),
            sa.Column("avg_cost", sa.Double(), nullable=False),
            sa.Column("market_value", sa.Double(), nullable=False),
            sa.Column("weight_pct", sa.Double(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    """No-op: these tables predate Alembic ownership and may hold real data.

    Dropping them on downgrade would destroy positions/targets/account history,
    so adoption is intentionally one-way. (The following d8589 downgrade still
    removes only the 4.9a *columns* it added, leaving the base tables intact.)
    """
    pass
