"""Coding suggestions cannot authorize spend; every line needs trusted provenance."""

import uuid

import pytest

from jev_invoice.db.models import Invoice, InvoiceLine
from jev_invoice.domain.enums import ReasonCode
from jev_invoice.pipeline.approval import approval_findings


def _line(source: str, account: str | None = "6000") -> InvoiceLine:
    return InvoiceLine(
        line_no=1,
        gl_account_code=account,
        gl_source=source,
        purchase_order_line_id=uuid.uuid4() if source == "po" else None,
    )


@pytest.mark.parametrize("source", ["ai", "vendor", "suspense", "person", ""])
def test_one_untrusted_line_blocks_auto_even_with_other_trusted_lines(source: str) -> None:
    invoice = Invoice(purchase_order_id=uuid.uuid4(), lines=[_line("po"), _line(source)])
    findings = approval_findings(invoice, suspense_account="9999")
    assert [f.code for f in findings] == [ReasonCode.GL_CODING_REVIEW]


@pytest.mark.parametrize("account", [None, "9999"])
def test_trusted_source_cannot_allow_missing_or_suspense_account(account: str | None) -> None:
    invoice = Invoice(purchase_order_id=uuid.uuid4(), lines=[_line("po", account)])
    assert (
        approval_findings(invoice, suspense_account="9999")[0].code == ReasonCode.GL_CODING_REVIEW
    )


def test_validated_correction_needs_no_gl_review_but_non_po_still_needs_budget_approval() -> None:
    invoice = Invoice(lines=[_line("validated_correction")])
    assert [f.code for f in approval_findings(invoice, suspense_account="9999")] == [
        ReasonCode.NON_PO_APPROVAL_REQUIRED
    ]
    invoice.purchase_order_id = uuid.uuid4()
    assert approval_findings(invoice, suspense_account="9999") == []


def test_empty_lines_and_unlinked_po_source_fail_closed() -> None:
    invoice = Invoice(purchase_order_id=uuid.uuid4(), lines=[])
    assert (
        approval_findings(invoice, suspense_account="9999")[0].code == ReasonCode.GL_CODING_REVIEW
    )
    line = _line("po")
    line.purchase_order_line_id = None
    invoice.lines = [line]
    assert (
        approval_findings(invoice, suspense_account="9999")[0].code == ReasonCode.GL_CODING_REVIEW
    )
