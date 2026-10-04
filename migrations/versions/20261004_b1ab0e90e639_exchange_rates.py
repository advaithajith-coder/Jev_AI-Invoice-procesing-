"""exchange rates

Revision ID: b1ab0e90e639
Revises: 8932077f7983
Create Date: 2026-10-04 19:49:39.448191
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1ab0e90e639"
down_revision: str | None = "8932077f7983"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exchange_rates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("requested_date", sa.Date(), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("rate", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint("rate > 0", name=op.f("ck_exchange_rates_exchange_rate_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_rates")),
        sa.UniqueConstraint(
            "currency", "base_currency", "requested_date", name=op.f("uq_exchange_rates_currency")
        ),
    )
    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("fx_rate", sa.Numeric(precision=18, scale=8), nullable=True))
        batch_op.add_column(sa.Column("fx_rate_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("fx_rate_source", sa.String(length=50), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.drop_column("fx_rate_source")
        batch_op.drop_column("fx_rate_date")
        batch_op.drop_column("fx_rate")

    op.drop_table("exchange_rates")
