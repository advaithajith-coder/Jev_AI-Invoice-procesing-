"""ORM models.

Conventions:
- Money is NUMERIC(18,2), quantities and unit prices NUMERIC(18,4). Never float.
- Enums are VARCHAR + CHECK (not native PG enums), so adding a value is a simple migration.
- Types are portable: SQLite for local development and tests, PostgreSQL in production
  (JSON becomes JSONB there). CHECK constraints avoid dialect-specific operators.
- `decisions` and `audit_events` are append-only; on PostgreSQL a trigger rejects
  UPDATE/DELETE/TRUNCATE (see the initial migration).
- Bank account numbers are never stored: only the last 4 characters and a keyed fingerprint.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jev_invoice.db.base import Base
from jev_invoice.domain.enums import (
    ActionBand,
    DecisionAction,
    DocumentType,
    GLAccountType,
    InvoiceSource,
    InvoiceStatus,
    PurchaseOrderStatus,
    ReviewStatus,
    VendorStatus,
)

Money = Numeric(18, 2)
Quantity = Numeric(18, 4)
UnitPrice = Numeric(18, 4)
JSONType = JSON().with_variant(JSONB(), "postgresql")
BigIntId = BigInteger().with_variant(Integer(), "sqlite")  # SQLite autoincrements INTEGER only


def enum_column(enum_cls: type[StrEnum]) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=enum_cls.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


def currency_check(column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} IS NULL OR (length({column}) = 3 AND upper({column}) = {column})",
        name=f"{column}_iso4217",
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── Master data ───────────────────────────────────────────────────────────────


class GLAccount(TimestampMixin, Base):
    """Chart of accounts. `description` is what Jev reads when choosing a GL code."""

    __tablename__ = "gl_accounts"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    account_type: Mapped[GLAccountType] = mapped_column(enum_column(GLAccountType))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())


class Vendor(TimestampMixin, Base):
    __tablename__ = "vendors"
    __table_args__ = (
        currency_check("default_currency"),
        CheckConstraint("payment_terms_days BETWEEN 0 AND 365", name="payment_terms_range"),
        CheckConstraint(
            "bank_account_last4 IS NULL OR length(bank_account_last4) = 4",
            name="bank_last4_length",
        ),
        CheckConstraint(
            "(early_pay_discount_pct IS NULL AND early_pay_days IS NULL)"
            " OR (early_pay_discount_pct > 0 AND early_pay_discount_pct < 100"
            " AND early_pay_days BETWEEN 1 AND 365)",
            name="early_pay_terms",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200), unique=True)
    tax_id: Mapped[str | None] = mapped_column(String(50))
    email_domain: Mapped[str | None] = mapped_column(String(255))
    default_currency: Mapped[str] = mapped_column(String(3))
    # Agreed terms: pay this many days after the invoice date, whatever the invoice prints.
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    # Early-payment discount, e.g. 2% if paid within 10 days ("2/10 net 30").
    early_pay_discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    early_pay_days: Mapped[int | None] = mapped_column(Integer)
    # False for vendors billed without a PO: rent, utilities, legal fees, subscriptions.
    po_required: Mapped[bool] = mapped_column(default=True, server_default=true())
    bank_account_last4: Mapped[str | None] = mapped_column(String(4))
    bank_account_fingerprint: Mapped[str | None] = mapped_column(String(64))
    default_gl_account_code: Mapped[str | None] = mapped_column(ForeignKey("gl_accounts.code"))
    status: Mapped[VendorStatus] = mapped_column(
        enum_column(VendorStatus), default=VendorStatus.ACTIVE
    )

    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="vendor")


class PurchaseOrder(TimestampMixin, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        currency_check("currency"),
        CheckConstraint("total_amount >= 0", name="total_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    po_number: Mapped[str] = mapped_column(String(50), unique=True)
    vendor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendors.id"), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[PurchaseOrderStatus] = mapped_column(enum_column(PurchaseOrderStatus))
    issued_date: Mapped[date] = mapped_column(Date)
    total_amount: Mapped[Decimal] = mapped_column(Money)

    vendor: Mapped[Vendor] = relationship(back_populates="purchase_orders")
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        back_populates="purchase_order",
        order_by="PurchaseOrderLine.line_no",
        cascade="all, delete-orphan",
    )
    receipts: Mapped[list["GoodsReceipt"]] = relationship(back_populates="purchase_order")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        UniqueConstraint("purchase_order_id", "line_no"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE")
    )
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Quantity)
    unit_price: Mapped[Decimal] = mapped_column(UnitPrice)
    gl_account_code: Mapped[str | None] = mapped_column(ForeignKey("gl_accounts.code"))

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class GoodsReceipt(TimestampMixin, Base):
    """Proof that goods or services on a PO were delivered (enables 3-way matching)."""

    __tablename__ = "goods_receipts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    receipt_number: Mapped[str] = mapped_column(String(50), unique=True)
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id"), index=True
    )
    received_date: Mapped[date] = mapped_column(Date)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="receipts")
    lines: Mapped[list["GoodsReceiptLine"]] = relationship(
        back_populates="goods_receipt", cascade="all, delete-orphan"
    )


class GoodsReceiptLine(Base):
    __tablename__ = "goods_receipt_lines"
    __table_args__ = (
        UniqueConstraint("goods_receipt_id", "purchase_order_line_id"),
        CheckConstraint("quantity_received > 0", name="quantity_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("goods_receipts.id", ondelete="CASCADE")
    )
    purchase_order_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_order_lines.id"), index=True
    )
    quantity_received: Mapped[Decimal] = mapped_column(Quantity)

    goods_receipt: Mapped[GoodsReceipt] = relationship(back_populates="lines")
    purchase_order_line: Mapped[PurchaseOrderLine] = relationship()


# ── Invoices ──────────────────────────────────────────────────────────────────


class Invoice(TimestampMixin, Base):
    """One received document. Extracted fields stay NULL until extraction succeeds."""

    __tablename__ = "invoices"
    __table_args__ = (
        currency_check("currency"),
        CheckConstraint("file_size_bytes > 0", name="file_size_positive"),
        Index("ix_invoices_vendor_number", "vendor_id", "invoice_number_normalized"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[InvoiceStatus] = mapped_column(
        enum_column(InvoiceStatus), default=InvoiceStatus.RECEIVED, index=True
    )
    source: Mapped[InvoiceSource] = mapped_column(enum_column(InvoiceSource))
    sender_email: Mapped[str | None] = mapped_column(String(320))

    # Original file
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    storage_key: Mapped[str] = mapped_column(String(500))

    # Matches against master data (set by checks, never by the LLM)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id"))
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_orders.id"), index=True
    )

    # Extracted fields
    document_type: Mapped[DocumentType | None] = mapped_column(enum_column(DocumentType))
    vendor_name: Mapped[str | None] = mapped_column(String(200))
    invoice_number: Mapped[str | None] = mapped_column(String(100))
    invoice_number_normalized: Mapped[str | None] = mapped_column(String(100))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(String(3))
    subtotal: Mapped[Decimal | None] = mapped_column(Money)
    discount: Mapped[Decimal | None] = mapped_column(Money)
    shipping: Mapped[Decimal | None] = mapped_column(Money)
    tax_total: Mapped[Decimal | None] = mapped_column(Money)
    # The invoice total, before anything already paid is subtracted.
    total: Mapped[Decimal | None] = mapped_column(Money)
    # Deposits, prepayments or credits already applied, and the balance left to pay.
    amount_paid: Mapped[Decimal | None] = mapped_column(Money)
    amount_due: Mapped[Decimal | None] = mapped_column(Money)
    po_reference: Mapped[str | None] = mapped_column(String(100))
    # What the invoice is for, and who it is addressed to.
    description: Mapped[str | None] = mapped_column(Text)
    bill_to_name: Mapped[str | None] = mapped_column(String(200))
    # The seller's tax ID as printed on the invoice (compared with the vendor's).
    vendor_tax_id: Mapped[str | None] = mapped_column(String(50))
    # As printed: payment terms, late-fee terms, remittance details (account numbers
    # masked to the last 4) and notes. {"payment_terms", "late_fee_terms", "remittance", "notes"}
    additional_info: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    bank_account_last4: Mapped[str | None] = mapped_column(String(4))
    bank_account_fingerprint: Mapped[str | None] = mapped_column(String(64))

    # Vision transcription (scans and photos only): what the model read from the image
    transcribed_text: Mapped[str | None] = mapped_column(Text)
    transcription_model: Mapped[str | None] = mapped_column(String(100))
    transcription_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))

    # Extraction provenance
    extraction_provider: Mapped[str | None] = mapped_column(String(50))
    extraction_model: Mapped[str | None] = mapped_column(String(100))
    extraction_prompt_version: Mapped[str | None] = mapped_column(String(20))
    extraction_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    raw_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    error_message: Mapped[str | None] = mapped_column(Text)

    # Exceptions. A hold (or a dispute with the vendor) stops export and payment until it
    # is released. A short-pay withholds part of the bill, with a reason.
    hold_kind: Mapped[str | None] = mapped_column(String(20))  # hold | dispute
    hold_category: Mapped[str | None] = mapped_column(String(40))
    hold_reason: Mapped[str | None] = mapped_column(Text)
    held_by: Mapped[str | None] = mapped_column(String(100))
    held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withheld_amount: Mapped[Decimal | None] = mapped_column(Money)
    withheld_reason: Mapped[str | None] = mapped_column(Text)

    export_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("export_batches.id"), index=True
    )
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    vendor: Mapped[Vendor | None] = relationship()
    purchase_order: Mapped[PurchaseOrder | None] = relationship()
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", order_by="InvoiceLine.line_no", cascade="all, delete-orphan"
    )
    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="invoice", order_by="Decision.created_at"
    )
    export_batch: Mapped["ExportBatch | None"] = relationship(back_populates="invoices")
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="invoice", order_by="Payment.paid_on", cascade="all, delete-orphan"
    )
    credits: Mapped[list["InvoiceCredit"]] = relationship(
        back_populates="invoice",
        foreign_keys="InvoiceCredit.invoice_id",
        order_by="InvoiceCredit.created_at",
        cascade="all, delete-orphan",
    )
    # Read-only: StepRecorder writes steps directly so they can be committed one by one.
    steps: Mapped[list["PipelineStep"]] = relationship(
        order_by="PipelineStep.position", viewonly=True
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (UniqueConstraint("invoice_id", "line_no"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Quantity)
    unit_price: Mapped[Decimal | None] = mapped_column(UnitPrice)
    amount: Mapped[Decimal | None] = mapped_column(Money)
    gl_account_code: Mapped[str | None] = mapped_column(ForeignKey("gl_accounts.code"))
    # How the account was chosen: po | ai | vendor | suspense | person
    gl_source: Mapped[str | None] = mapped_column(String(20))
    # Jev's confidence in its choice (0..1), when it chose or was asked
    gl_confidence: Mapped[float | None] = mapped_column()
    purchase_order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("purchase_order_lines.id")
    )

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class Payment(Base):
    """Money paid against an approved invoice (recorded here, paid in the bank).

    `discount_taken` is the early-payment discount kept when paying inside the window:
    it settles part of the bill without being paid.
    """

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("discount_taken >= 0", name="discount_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    paid_on: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    discount_taken: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    method: Mapped[str] = mapped_column(String(20))  # ach | check | wire | card
    reference: Mapped[str | None] = mapped_column(String(100))
    recorded_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class InvoiceCredit(Base):
    """A vendor credit applied to an invoice, reducing what is owed on it.

    Either an uploaded credit note (`credit_note_id`, itself an invoice row whose
    document type is credit_note) or one entered by hand from the vendor's number.
    """

    __tablename__ = "invoice_credits"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    credit_note_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"), unique=True)
    number: Mapped[str] = mapped_column(String(100))
    amount: Mapped[Decimal] = mapped_column(Money)
    reason: Mapped[str | None] = mapped_column(Text)
    recorded_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invoice: Mapped[Invoice] = relationship(back_populates="credits", foreign_keys=[invoice_id])
    credit_note: Mapped[Invoice | None] = relationship(foreign_keys=[credit_note_id])


# ── Decisions, review, audit ──────────────────────────────────────────────────


class Decision(Base):
    """One policy decision for an invoice. Append-only: a re-run adds a new row.

    Stores what is needed to audit and replay it: code findings, raw Jev answers,
    the versions of model, question set, and policy, and a hash of all inputs.
    """

    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[DecisionAction] = mapped_column(enum_column(DecisionAction))
    band: Mapped[ActionBand] = mapped_column(enum_column(ActionBand))
    reason_codes: Mapped[list[str]] = mapped_column(JSONType, default=list)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    rationale: Mapped[str] = mapped_column(Text)
    jev_status: Mapped[str] = mapped_column(String(20))
    jev_answers: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    jev_model: Mapped[str | None] = mapped_column(String(50))
    jev_latency_ms: Mapped[float | None] = mapped_column()
    question_set_version: Mapped[str] = mapped_column(String(20))
    policy_version: Mapped[str] = mapped_column(String(20))
    inputs_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invoice: Mapped[Invoice] = relationship(back_populates="decisions")


class ReviewTask(TimestampMixin, Base):
    __tablename__ = "review_tasks"
    __table_args__ = (
        # At most one open review per invoice.
        Index(
            "uq_review_tasks_open_invoice",
            "invoice_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), index=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("decisions.id"))
    status: Mapped[ReviewStatus] = mapped_column(
        enum_column(ReviewStatus), default=ReviewStatus.OPEN
    )
    assigned_to: Mapped[str | None] = mapped_column(String(100))
    resolved_by: Mapped[str | None] = mapped_column(String(100))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)

    invoice: Mapped[Invoice] = relationship()
    decision: Mapped[Decision] = relationship()


class PipelineStep(Base):
    """One step of processing one invoice: what ran, how long it took, what came out.

    Written as the step starts (status "running") and updated when it ends, so screens
    can show an invoice moving through the pipeline while it happens.
    """

    __tablename__ = "pipeline_steps"
    __table_args__ = (UniqueConstraint("invoice_id", "step"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    step: Mapped[str] = mapped_column(String(40))
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))  # running | done | failed | skipped
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column()
    summary: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class ExportBatch(Base):
    """One export file handed to an accounting system. Each invoice is exported once."""

    __tablename__ = "export_batches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    format: Mapped[str] = mapped_column(String(40))
    filename: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(100))
    invoice_count: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="export_batch")


class AppSetting(Base):
    """A business rule changed from the screens, such as the approval limit.

    Values are text (each setting parses its own); who changed what goes to the audit trail.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))
    updated_by: Mapped[str] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditEvent(Base):
    """Immutable log of who did what, when. Append-only."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_entity", "entity_type", "entity_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigIntId, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
