"""Persist per-exception ownership recommendations."""

import sqlalchemy as sa
from alembic import op

revision = "c5b09d1e7f32"
down_revision = "f2c1a0b9d734"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("decisions", sa.Column("exception_routing", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("decisions") as batch:
        batch.drop_column("exception_routing")
