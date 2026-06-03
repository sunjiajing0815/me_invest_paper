"""add temperature and cache tiers to llm_call_log

Revision ID: dec2737608db
Revises: fbdf8f40c65a
Create Date: 2026-06-03 11:30:14.545414

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dec2737608db'
down_revision: Union[str, Sequence[str], None] = 'fbdf8f40c65a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add temperature + prompt-cache tier columns to llm_call_log.

    SQLite supports ADD COLUMN with a DEFAULT directly, so no table rebuild is needed.
    temperature is nullable (NULL backfills existing rows + agent_sdk calls that can't set
    it). cache_write/read_tokens are NOT NULL with server_default '0' so existing rows
    (all pre-caching) backfill to 0.
    """
    op.add_column(
        "llm_call_log",
        sa.Column("temperature", sa.Float(), nullable=True),
    )
    op.add_column(
        "llm_call_log",
        sa.Column(
            "cache_write_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "llm_call_log",
        sa.Column(
            "cache_read_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    """Drop the three columns (SQLite needs batch mode to drop a column)."""
    with op.batch_alter_table("llm_call_log") as batch:
        batch.drop_column("cache_read_tokens")
        batch.drop_column("cache_write_tokens")
        batch.drop_column("temperature")
