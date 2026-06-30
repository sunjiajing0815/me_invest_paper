"""add funds_event

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-30 00:00:00.000000

P2.3: append-only record of detected external cash flows (deposit/withdrawal). See ADR-0035.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "funds_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("broker_account_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delta_usd", sa.Double(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("prev_cash", sa.Double(), nullable=False),
        sa.Column("cur_cash", sa.Double(), nullable=False),
        sa.Column("trade_cash_flow", sa.Double(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("funds_event")
