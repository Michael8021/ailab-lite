"""initial schema: the tables Base.metadata.create_all() used to build

Databases created before migrations existed are stamped with this revision (see migrate.py).

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_email"), ["email"], unique=True)

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_api_tokens_token_hash"), ["token_hash"], unique=True)
        batch_op.create_index(batch_op.f("ix_api_tokens_user_id"), ["user_id"], unique=False)

    op.create_table(
        "files",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.String(length=40), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["files.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_files_user_id"), ["user_id"], unique=False)

    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("input_file_ids", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runs_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_user_id"), ["user_id"], unique=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("file_id", sa.String(length=40), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_artifacts_run_id"), ["run_id"], unique=False)

    op.create_table(
        "model_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("model_usage", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_model_usage_run_id"), ["run_id"], unique=False)

    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_event_seq"),
    )
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_events_run_id"), ["run_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("run_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_events_run_id"))

    op.drop_table("run_events")
    with op.batch_alter_table("model_usage", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_model_usage_run_id"))

    op.drop_table("model_usage")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifacts_run_id"))

    op.drop_table("artifacts")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_user_id"))
        batch_op.drop_index(batch_op.f("ix_runs_status"))

    op.drop_table("runs")
    with op.batch_alter_table("files", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_files_user_id"))

    op.drop_table("files")
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_tokens_user_id"))
        batch_op.drop_index(batch_op.f("ix_api_tokens_token_hash"))

    op.drop_table("api_tokens")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_email"))

    op.drop_table("users")
