"""indexes for the per-user lists and quota scans

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_runs_user_created", "runs", ["user_id", "created_at"])
    op.create_index("ix_files_user_created", "files", ["user_id", "created_at"])
    op.create_index("ix_conversations_user_updated", "conversations", ["user_id", "updated_at"])
    op.create_index("ix_model_usage_run_created", "model_usage", ["run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_model_usage_run_created", table_name="model_usage")
    op.drop_index("ix_conversations_user_updated", table_name="conversations")
    op.drop_index("ix_files_user_created", table_name="files")
    op.drop_index("ix_runs_user_created", table_name="runs")
