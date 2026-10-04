"""The lifecycle rail: which of received, checked, approved, exported, paid an invoice has
reached, and the tone and caption of the step it is waiting at."""

from datetime import date
from decimal import Decimal

from jev_invoice.db.models import Invoice, Payment
from jev_invoice.domain.enums import DocumentType, InvoiceStatus
from jev_invoice.web.lifecycle import lifecycle


def _invoice(status: InvoiceStatus, **values: object) -> Invoice:
    invoice = Invoice(status=status, total=Decimal("100.00"), payments=[], credits=[], **values)
    return invoice


def _states(invoice: Invoice) -> list[str]:
    return [state for _, state in lifecycle(invoice).steps]


def test_waiting_for_review_is_amber_at_approval() -> None:
    rail = lifecycle(_invoice(InvoiceStatus.PENDING_REVIEW))

    assert rail.tone == "warn" and rail.caption == "Waiting for review"
    assert [state for _, state in rail.steps] == ["done", "done", "current", "todo", "todo"]
    assert rail.label == "Received, checked. Waiting for review."


def test_approved_exported_and_held() -> None:
    assert lifecycle(_invoice(InvoiceStatus.APPROVED)).caption == "Ready to export"
    assert _states(_invoice(InvoiceStatus.EXPORTED))[3:] == ["done", "current"]
    held = lifecycle(_invoice(InvoiceStatus.APPROVED, hold_kind="dispute"))
    assert held.tone == "warn" and held.caption == "Disputed"


def test_paid_finishes_the_rail_in_green() -> None:
    invoice = _invoice(InvoiceStatus.EXPORTED)
    invoice.payments = [
        Payment(amount=Decimal("100.00"), discount_taken=Decimal(0), paid_on=date(2026, 10, 1))
    ]

    rail = lifecycle(invoice)

    assert rail.tone == "good" and rail.caption == "Paid"
    assert set(_states(invoice)) == {"done"}


def test_stopped_and_unreadable_are_red() -> None:
    assert lifecycle(_invoice(InvoiceStatus.REJECTED)).tone == "bad"
    failed = lifecycle(_invoice(InvoiceStatus.FAILED))
    assert failed.tone == "bad" and failed.caption == "Could not be read"


def test_applied_credit_note() -> None:
    rail = lifecycle(_invoice(InvoiceStatus.APPROVED, document_type=DocumentType.CREDIT_NOTE))

    assert rail.caption == "Credit applied" and "current" not in _states(
        _invoice(InvoiceStatus.APPROVED, document_type=DocumentType.CREDIT_NOTE)
    )
