"""add meta table

Revision ID: 71b1bd302b7e
Revises: 6c9b40ddd25c
Create Date: 2026-04-28 14:19:05.886384

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '71b1bd302b7e'
down_revision: Union[str, Sequence[str], None] = '6c9b40ddd25c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "meta",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("meta")
