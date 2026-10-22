"""user settings: per-user preferences such as the skills they switched off

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("settings", sa.JSON(), nullable=True))  # plain ADD COLUMN, no rebuild


def downgrade() -> None:
    op.drop_column("users", "settings")
