"""API request and response models. Built from ORM rows; never expose raw bank accounts."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from jev_invoice.db.models import Decision, Invoice, ReviewTask


class LineOut(BaseModel):
    line_no: int
    description: str
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None


class FindingOut(BaseModel):
    code: str
    message: str


class DecisionOut(BaseModel):
    id: uuid.UUID
    action: str
    band: str
    reason_codes: list[str]
    findings: list[FindingOut]
    rationale: str
    jev_status: str
    jev_answers: dict[str, Any] | None
    jev_model: str | None
    question_set_version: str
    policy_version: str
    created_at: datetime


class TranscriptionOut(BaseModel):
    """What the vision model read from a scan or photo."""

    model: str | None
    text: str
    cost_usd: Decimal | None


class ExtractionOut(BaseModel):
    provider: str | None
    model: str | None
    prompt_version: str | None
    cost_usd: Decimal | None


class InvoiceOut(BaseModel):
    id: uuid.UUID
    status: str
    source: str
    original_filename: str
    received_at: datetime
    already_received: bool = Field(
        default=False, description="True when this exact file was uploaded before"
    )
    document_type: str | None
    vendor_name: str | None
    vendor_matched: bool
    invoice_number: str | None
    invoice_date: date | None
    due_date: date | None
    currency: str | None
    description: str | None
    bill_to_name: str | None
    vendor_tax_id: str | None
    subtotal: Decimal | None
    discount: Decimal | None
    shipping: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    amount_paid: Decimal | None
    amount_due: Decimal | None
    po_reference: str | None
    purchase_order_number: str | None
    bank_account_last4: str | None
    lines: list[LineOut]
    additional_info: dict[str, Any]
    """As printed: payment terms, late-fee terms, remittance (accounts masked), notes."""
    transcription: TranscriptionOut | None
    extraction: ExtractionOut | None
    decision: DecisionOut | None
    error_message: str | None


class ReviewTaskOut(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime
    resolved_by: str | None
    resolved_at: datetime | None
    resolution_note: str | None
    invoice: InvoiceOut


class ResolveReviewIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100, description="Who is making the call")
    note: str | None = Field(default=None, max_length=2000)


def decision_out(decision: Decision) -> DecisionOut:
    return DecisionOut(
        id=decision.id,
        action=decision.action,
        band=decision.band,
        reason_codes=list(decision.reason_codes),
        findings=[FindingOut(**finding) for finding in decision.findings],
        rationale=decision.rationale,
        jev_status=decision.jev_status,
        jev_answers=decision.jev_answers,
        jev_model=decision.jev_model,
        question_set_version=decision.question_set_version,
        policy_version=decision.policy_version,
        created_at=decision.created_at,
    )


def invoice_out(invoice: Invoice, *, already_received: bool = False) -> InvoiceOut:
    latest = invoice.decisions[-1] if invoice.decisions else None
    return InvoiceOut(
        id=invoice.id,
        status=invoice.status,
        source=invoice.source,
        original_filename=invoice.original_filename,
        received_at=invoice.received_at,
        already_received=already_received,
        document_type=invoice.document_type,
        vendor_name=invoice.vendor.name if invoice.vendor else invoice.vendor_name,
        vendor_matched=invoice.vendor is not None,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        currency=invoice.currency,
        description=invoice.description,
        bill_to_name=invoice.bill_to_name,
        vendor_tax_id=invoice.vendor_tax_id,
        subtotal=invoice.subtotal,
        discount=invoice.discount,
        shipping=invoice.shipping,
        tax=invoice.tax_total,
        total=invoice.total,
        amount_paid=invoice.amount_paid,
        amount_due=invoice.amount_due,
        additional_info=invoice.additional_info or {},
        po_reference=invoice.po_reference,
        purchase_order_number=invoice.purchase_order.po_number if invoice.purchase_order else None,
        bank_account_last4=invoice.bank_account_last4,
        lines=[
            LineOut(
                line_no=line.line_no,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
            )
            for line in invoice.lines
        ],
        transcription=(
            TranscriptionOut(
                model=invoice.transcription_model,
                text=invoice.transcribed_text,
                cost_usd=invoice.transcription_cost_usd,
            )
            if invoice.transcribed_text
            else None
        ),
        extraction=(
            ExtractionOut(
                provider=invoice.extraction_provider,
                model=invoice.extraction_model,
                prompt_version=invoice.extraction_prompt_version,
                cost_usd=invoice.extraction_cost_usd,
            )
            if invoice.extraction_model
            else None
        ),
        decision=decision_out(latest) if latest else None,
        error_message=invoice.error_message,
    )


def review_task_out(task: ReviewTask) -> ReviewTaskOut:
    return ReviewTaskOut(
        id=task.id,
        status=task.status,
        created_at=task.created_at,
        resolved_by=task.resolved_by,
        resolved_at=task.resolved_at,
        resolution_note=task.resolution_note,
        invoice=invoice_out(task.invoice),
    )
