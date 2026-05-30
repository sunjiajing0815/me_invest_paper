"""phase4_9a b9 broker_account_id not null

Tightens the multi-broker partition key to NOT NULL now that every writer sets it
(B1–B8): broker_account_id on the four per-account tables, and account_ref on
broker_account. d8589 backfilled all pre-existing rows, so this is safe to apply
after the chain runs. SQLite can't ALTER a column to NOT NULL in place — batch mode
(render_as_batch=True) recreates each table, preserving its indexes/constraints.

Revision ID: 6a4a9fada1dc
Revises: d8589fe198cf
Create Date: 2026-05-30 15:39:05.081049

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6a4a9fada1dc'
down_revision: str | Sequence[str] | None = 'd8589fe198cf'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PER_ACCOUNT_TABLES = (
    "target_allocation",
    "positions_snapshot",
    "order_suggestion",
    "order_execution",
)


def upgrade() -> None:
    """Flip the partition columns to NOT NULL (all rows backfilled by d8589)."""
    with op.batch_alter_table("broker_account", schema=None) as b:
        b.alter_column("account_ref", existing_type=sa.Integer(), nullable=False)
    for tbl in _PER_ACCOUNT_TABLES:
        with op.batch_alter_table(tbl, schema=None) as b:
            b.alter_column("broker_account_id", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    """Relax the partition columns back to nullable."""
    for tbl in _PER_ACCOUNT_TABLES:
        with op.batch_alter_table(tbl, schema=None) as b:
            b.alter_column("broker_account_id", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("broker_account", schema=None) as b:
        b.alter_column("account_ref", existing_type=sa.Integer(), nullable=True)
