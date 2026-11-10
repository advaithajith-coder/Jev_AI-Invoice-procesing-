"""Persist PO finance details and invoice classification provenance."""

import sqlalchemy as sa
from alembic import op

revision = "f2c1a0b9d734"
down_revision = "48bd549f68b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("purchase_order_lines", sa.Column("finance_details", sa.JSON(), nullable=True))
    op.add_column("invoice_lines", sa.Column("finance_classification", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invoice_lines") as batch:
        batch.drop_column("finance_classification")
    with op.batch_alter_table("purchase_order_lines") as batch:
        batch.drop_column("finance_details")
