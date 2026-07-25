"""add reflection_insight table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-25 00:00:00.000000

Weekly-review reflection lessons (plans/pre_phase5_features_design.md §4): the
accumulating methodology "wisdom log". Methodology observations only — never flows
back into the suggestion engine.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reflection_insight",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("broker_account_id", sa.Integer(), nullable=False),
        sa.Column("week_of", sa.Date(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("lesson", sa.Text(), nullable=False),
        sa.Column("tickers", sa.Text(), nullable=False),
        sa.Column("relation_to_prior", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("reflection_insight")
