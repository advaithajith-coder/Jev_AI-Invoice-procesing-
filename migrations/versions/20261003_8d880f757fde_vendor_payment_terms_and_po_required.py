"""vendor payment terms and po required

Revision ID: 8d880f757fde
Revises: e178bf2a862c
Create Date: 2026-10-03 03:31:22.137450
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d880f757fde"
down_revision: str | None = "e178bf2a862c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vendors", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("early_pay_discount_pct", sa.Numeric(precision=5, scale=2), nullable=True)
        )
        batch_op.add_column(sa.Column("early_pay_days", sa.Integer(), nullable=True))
        # sa.true() renders as 1 on SQLite and true on PostgreSQL.
        batch_op.add_column(
            sa.Column("po_required", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch_op.create_check_constraint(
            op.f("ck_vendors_early_pay_terms"),
            "(early_pay_discount_pct IS NULL AND early_pay_days IS NULL)"
            " OR (early_pay_discount_pct > 0 AND early_pay_discount_pct < 100"
            " AND early_pay_days BETWEEN 1 AND 365)",
        )


def downgrade() -> None:
    with op.batch_alter_table("vendors", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_vendors_early_pay_terms"), type_="check")
        batch_op.drop_column("po_required")
        batch_op.drop_column("early_pay_days")
        batch_op.drop_column("early_pay_discount_pct")
