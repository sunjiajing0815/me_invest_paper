"""add cancelled_at to order_execution

Revision ID: a1b2c3d4e5f6
Revises: dec2737608db
Create Date: 2026-06-30 00:00:00.000000

P1.3: timestamp set when an execution's status is flipped to 'broker_cancelled'
(services/reconciliation.py::sync_open_order_statuses). Drives the manual-broker-UI-cancel
inference that flips a still-'accepted' suggestion to 'cancelled' after a grace window.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'dec2737608db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable cancelled_at (SQLite ADD COLUMN; NULL backfills existing rows)."""
    op.add_column(
        "order_execution",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop cancelled_at (SQLite needs batch mode to drop a column)."""
    with op.batch_alter_table("order_execution") as batch:
        batch.drop_column("cancelled_at")
