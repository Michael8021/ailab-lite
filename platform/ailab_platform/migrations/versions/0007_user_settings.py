"""user settings: per-user preferences such as the skills they switched off

Also safe on a database that ran an earlier, unreleased numbering of this revision (as 0006, so it never got
0006's indexes): the column is only added when missing and the 0006 indexes are created when missing.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES_0006 = {
    "ix_runs_user_created": ("runs", ["user_id", "created_at"]),
    "ix_files_user_created": ("files", ["user_id", "created_at"]),
    "ix_conversations_user_updated": ("conversations", ["user_id", "updated_at"]),
    "ix_model_usage_run_created": ("model_usage", ["run_id", "created_at"]),
}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "settings" not in {c["name"] for c in inspector.get_columns("users")}:
        op.add_column("users", sa.Column("settings", sa.JSON(), nullable=True))  # plain ADD COLUMN, no rebuild
    for name, (table, columns) in INDEXES_0006.items():
        if name not in {i["name"] for i in inspector.get_indexes(table)}:
            op.create_index(name, table, columns)


def downgrade() -> None:
    op.drop_column("users", "settings")
