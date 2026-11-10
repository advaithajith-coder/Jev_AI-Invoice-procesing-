"""Track invoice exception ownership and resolution."""

import sqlalchemy as sa
from alembic import op

revision = "e4a917c38b62"
down_revision = "d872d067a901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoice_exceptions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("invoice_id", sa.Uuid(), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("assigned_to", sa.String(100)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("resolution_reason", sa.Text()),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("invoice_id", "code"),
    )
    op.create_index("ix_invoice_exceptions_invoice_id", "invoice_exceptions", ["invoice_id"])


def downgrade() -> None:
    op.drop_table("invoice_exceptions")
