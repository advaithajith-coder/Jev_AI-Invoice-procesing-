"""Approved accounting combinations."""

import sqlalchemy as sa
from alembic import op

revision = "d872d067a901"
down_revision = "c5b09d1e7f32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("accounting_rules")
