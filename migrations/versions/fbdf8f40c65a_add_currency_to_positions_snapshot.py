"""add currency to positions_snapshot

Revision ID: fbdf8f40c65a
Revises: 6a4a9fada1dc
Create Date: 2026-06-01 09:39:33.041796

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fbdf8f40c65a'
down_revision: Union[str, Sequence[str], None] = '6a4a9fada1dc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the per-position native-currency column (default USD for existing rows).

    SQLite supports ADD COLUMN with a DEFAULT directly, so no table rebuild is needed;
    existing rows (all Alpaca, plus historical Moomoo) backfill to 'USD'.
    """
    op.add_column(
        "positions_snapshot",
        sa.Column("currency", sa.String(), nullable=False, server_default="USD"),
    )


def downgrade() -> None:
    """Drop the currency column (SQLite needs batch mode to drop a column)."""
    with op.batch_alter_table("positions_snapshot") as batch:
        batch.drop_column("currency")
