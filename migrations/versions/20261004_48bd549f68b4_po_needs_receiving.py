"""po needs receiving

Revision ID: 48bd549f68b4
Revises: b1ab0e90e639
Create Date: 2026-10-04 23:17:41.085186
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "48bd549f68b4"
down_revision: str | None = "b1ab0e90e639"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("purchase_orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("needs_receiving", sa.Boolean(), server_default=sa.true(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("purchase_orders", schema=None) as batch_op:
        batch_op.drop_column("needs_receiving")
