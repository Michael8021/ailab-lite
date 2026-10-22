"""conversations: chat threads whose turns are runs

A run can now belong to a conversation (``runs.conversation_id``); runs created before stay standalone.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    # A first attempt of this revision (batch mode rebuilt `runs`, which SQLite refuses while other tables reference
    # its rows) could stop half way, and SQLite DDL is not rolled back: clear what it left behind.
    if "_alembic_tmp_runs" in tables:
        op.drop_table("_alembic_tmp_runs")
    if "conversations" not in tables:
        op.create_table(
            "conversations",
            sa.Column("id", sa.String(length=40), nullable=False),
            sa.Column("user_id", sa.String(length=40), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("model", sa.String(length=120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_conversations_user_id"), "conversations", ["user_id"], unique=False)
    # A plain ADD COLUMN, never a table rebuild: `runs` is referenced by run_events, artifacts and model_usage.
    # SQLite takes the REFERENCES clause inline; PostgreSQL gets a named constraint.
    if bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE runs ADD COLUMN conversation_id VARCHAR(40) REFERENCES conversations (id)")
    else:
        op.add_column("runs", sa.Column("conversation_id", sa.String(length=40), nullable=True))
        op.create_foreign_key("fk_runs_conversation_id", "runs", "conversations", ["conversation_id"], ["id"])
    op.create_index(op.f("ix_runs_conversation_id"), "runs", ["conversation_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_runs_conversation_id"), table_name="runs")
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE runs DROP COLUMN conversation_id")  # SQLite >= 3.35; no table rebuild
    else:
        op.drop_constraint("fk_runs_conversation_id", "runs", type_="foreignkey")
        op.drop_column("runs", "conversation_id")
    op.drop_index(op.f("ix_conversations_user_id"), table_name="conversations")
    op.drop_table("conversations")
