"""sqlite switchover

Revision ID: 0047fb7675f2
Revises: 71b1bd302b7e
Create Date: 2026-04-28 18:36:49.405511

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0047fb7675f2'
down_revision: Union[str, Sequence[str], None] = '71b1bd302b7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
