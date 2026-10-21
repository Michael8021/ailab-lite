"""usage ledger: weight and cached tokens

``model_usage`` now records, per call, the model's quota weight at that time and how many prompt tokens
came from the upstream's cache. Before this, the weight was looked up in models.toml when the quota was
*read*, so editing a weight silently rewrote the cost of every past call.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_usage", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cached_tokens", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("weight", sa.Float(), server_default="1.0", nullable=False))

    # Existing rows were charged at the catalog weight of their model as it stands now (the old behaviour),
    # so freeze exactly that value instead of letting everyone's used quota drop to weight 1.
    try:
        from ailab_platform.catalog import get_catalog

        weights = {m.id: m.weight for m in get_catalog().models.values()}
    except Exception:  # no catalog available (offline tooling): keep the default weight of 1
        weights = {}
    usage = sa.table("model_usage", sa.column("model", sa.String), sa.column("weight", sa.Float))
    for model, weight in weights.items():
        op.execute(usage.update().where(usage.c.model == model).values(weight=weight))


def downgrade() -> None:
    with op.batch_alter_table("model_usage", schema=None) as batch_op:
        batch_op.drop_column("weight")
        batch_op.drop_column("cached_tokens")
