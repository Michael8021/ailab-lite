"""Projects and file library organization.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])
    op.create_index("ix_projects_user_updated", "projects", ["user_id", "updated_at"])
    for table in ("conversations", "files"):
        # These tables have inbound references; SQLite must not rebuild them.
        if op.get_bind().dialect.name == "sqlite":
            op.execute(f"ALTER TABLE {table} ADD COLUMN project_id VARCHAR(40) REFERENCES projects (id)")
        else:
            op.add_column(table, sa.Column("project_id", sa.String(40), nullable=True))
            op.create_foreign_key(f"fk_{table}_project_id", table, "projects", ["project_id"], ["id"])
        op.create_index(f"ix_{table}_project_id", table, ["project_id"])
    op.add_column("files", sa.Column("starred_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_files_user_starred",
        "files",
        ["user_id", "starred_at"],
        sqlite_where=sa.text("starred_at IS NOT NULL"),
        postgresql_where=sa.text("starred_at IS NOT NULL"),
    )
    op.create_index("ix_files_user_project_created", "files", ["user_id", "project_id", "created_at"])
    op.create_index(
        "ix_files_images_user_created",
        "files",
        ["user_id", "created_at"],
        sqlite_where=sa.text("content_type LIKE 'image/%'"),
        postgresql_where=sa.text("content_type LIKE 'image/%'"),
    )
    op.create_index(
        "ix_conversations_user_project_live",
        "conversations",
        ["user_id", "project_id"],
        sqlite_where=sa.text("deleted_at IS NULL AND project_id IS NOT NULL"),
        postgresql_where=sa.text("deleted_at IS NULL AND project_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_files_images_user_created", table_name="files")
    op.drop_index("ix_conversations_user_project_live", table_name="conversations")
    op.drop_index("ix_files_user_project_created", table_name="files")
    op.drop_index("ix_files_user_starred", table_name="files")
    op.drop_column("files", "starred_at")
    for table in ("files", "conversations"):
        op.drop_index(f"ix_{table}_project_id", table_name=table)
        if op.get_bind().dialect.name == "sqlite":
            op.execute(f"ALTER TABLE {table} DROP COLUMN project_id")
        else:
            op.drop_constraint(f"fk_{table}_project_id", table, type_="foreignkey")
            op.drop_column(table, "project_id")
    op.drop_index("ix_projects_user_updated", table_name="projects")
    op.drop_index("ix_projects_user_id", table_name="projects")
    op.drop_table("projects")
