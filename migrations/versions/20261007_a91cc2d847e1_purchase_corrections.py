"""Add scoped, validated purchase corrections."""

import sqlalchemy as sa
from alembic import op

revision = "a91cc2d847e1"
down_revision = "e4a917c38b62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "purchase_corrections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("vendor_id", sa.Uuid(), sa.ForeignKey("vendors.id"), nullable=False),
        sa.Column("source_invoice_id", sa.Uuid(), sa.ForeignKey("invoices.id"), nullable=False),
        sa.Column("source_line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("description_key", sa.String(64), nullable=False),
        sa.Column("buyer_key", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("field", sa.String(30), nullable=False),
        sa.Column("value", sa.String(100), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("source_section", sa.String(100)),
        sa.Column("source_version", sa.String(50)),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("validated_by", sa.String(100)),
        sa.Column("validation_reason", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'withdrawn')", name="correction_status"
        ),
    )
    op.create_index(
        "ix_correction_scope", "purchase_corrections", ["vendor_id", "description_key", "status"]
    )


def downgrade() -> None:
    op.drop_table("purchase_corrections")
