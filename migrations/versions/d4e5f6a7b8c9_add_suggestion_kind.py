"""add order_suggestion.kind + is_highlighted

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-19 00:00:00.000000

Top-up suggestions (plans/topup_suggestions_design.md): kind partitions regular
rebalancing suggestions from sentiment-sized near-target top-ups; is_highlighted marks
the deterministic strong-entry flag. Existing rows backfill to ('regular', 0).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("order_suggestion") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(), nullable=False, server_default="regular")
        )
        batch.add_column(
            sa.Column("is_highlighted", sa.Boolean(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("order_suggestion") as batch:
        batch.drop_column("is_highlighted")
        batch.drop_column("kind")
