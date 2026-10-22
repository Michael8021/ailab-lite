"""run options: the mode (chat / work) and skill the user picked for a message

A plain nullable ADD COLUMN: no table rebuild (see 0004 for why that matters on SQLite).

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("options", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "options")  # SQLite >= 3.35: a plain DROP COLUMN
