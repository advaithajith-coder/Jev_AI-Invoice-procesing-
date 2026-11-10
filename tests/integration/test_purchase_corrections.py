"""Scoped correction reuse must never turn an invoice approval into global learning."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jev_invoice.coding.corrections import CorrectionError, matching, propose, set_status
from jev_invoice.coding.service import CodingService
from jev_invoice.config import Settings
from jev_invoice.db.models import Invoice, InvoiceLine, PurchaseCorrection, PurchaseOrder, Vendor
from jev_invoice.domain.enums import InvoiceSource, InvoiceStatus, ReasonCode
from jev_invoice.pipeline.approval import approval_findings
from jev_invoice.tax.massachusetts import check_tax
from tests.fakes import acme_invoice_fields
from tests.integration.test_coding import _process


async def item(session: AsyncSession) -> tuple[Invoice, Vendor]:
    vendor = (await session.scalars(select(Vendor))).first()
    assert vendor is not None
    today = datetime.now(UTC).date()
    invoice = Invoice(
        source=InvoiceSource.UPLOAD,
        vendor_id=vendor.id,
        vendor_name=vendor.name,
        bill_to_name="Northwind Analytics Inc",
        currency="USD",
        invoice_date=today,
        original_filename="test.txt",
        content_type="text/plain",
        file_size_bytes=1,
        file_sha256=uuid.uuid4().hex * 2,
        storage_key="test",
        subtotal=Decimal("100"),
        tax_total=Decimal("6.25"),
        total=Decimal("106.25"),
        discount=Decimal("0"),
        shipping=Decimal("0"),
        lines=[InvoiceLine(line_no=1, description="Printer paper", amount=Decimal("100"))],
    )
    session.add(invoice)
    await session.flush()
    return invoice, vendor


async def correction(
    session: AsyncSession, invoice: Invoice, field: str, value: str, source: str | None = None
) -> PurchaseCorrection:
    today = datetime.now(UTC).date()
    return await propose(
        session,
        invoice,
        invoice.lines[0],
        field=field,
        value=value,
        evidence="Reviewed the purchase line and supporting policy.",
        person="Reviewer",
        valid_from=today - timedelta(days=1),
        valid_until=today + timedelta(days=30),
        source_section=source,
    )


async def matches(
    session: AsyncSession, invoice: Invoice
) -> dict[int, dict[str, PurchaseCorrection]]:
    return await matching(
        session,
        vendor_id=invoice.vendor_id,
        buyer=invoice.bill_to_name,
        currency=invoice.currency,
        invoice_date=invoice.invoice_date,
        lines=invoice.lines,
    )


async def test_learned_gl_requires_two_consistent_bills_and_finance_review(
    session: AsyncSession,
) -> None:
    invoice, vendor = await item(session)
    first, _ = await item(session)
    second, _ = await item(session)
    assert invoice.invoice_date is not None
    for historical in (first, second):
        historical.invoice_date = invoice.invoice_date - timedelta(days=1)
        historical.lines[0].gl_account_code = "6100"
        historical.lines[0].gl_source = "person"
    first.status = InvoiceStatus.APPROVED
    await session.flush()
    service = CodingService(session, None, suspense_account="9999", min_confidence=0.6)

    async def code() -> None:
        await service.code(
            invoice.lines,
            vendor=vendor,
            purchase_order=None,
            invoice_description=None,
            buyer=invoice.bill_to_name,
            currency=invoice.currency,
            invoice_date=invoice.invoice_date,
        )

    await code()
    assert invoice.lines[0].gl_source != "learned_correction"
    second.status = InvoiceStatus.EXPORTED
    await session.flush()
    await code()
    line = invoice.lines[0]
    assert line.gl_source == "learned_correction"
    assert line.gl_account_code == "6100"
    evidence = (line.finance_classification or {})["fields"]["gl_account"]
    assert set(evidence["source_invoice_ids"]) == {str(first.id), str(second.id)}
    assert ReasonCode.GL_CODING_REVIEW in {
        finding.code for finding in approval_findings(invoice, suspense_account="9999")
    }
    second.lines[0].gl_account_code = "9999"
    await session.flush()
    await code()
    assert line.gl_source != "learned_correction"


async def test_pending_active_scope_and_withdrawal(session: AsyncSession) -> None:
    invoice, _ = await item(session)
    row = await correction(session, invoice, "purchase_category", "goods")
    assert not await matches(session, invoice)
    await set_status(session, row.id, activate=True, person="Finance", reason="Evidence checked")
    assert (await matches(session, invoice))[1]["purchase_category"].value == "goods"
    invoice.lines[0].description = "Printer paper and consulting"
    assert not await matches(session, invoice)
    invoice.lines[0].description = "  PRINTER  paper "
    assert await matches(session, invoice)
    invoice.bill_to_name = "Another entity"
    assert not await matches(session, invoice)
    invoice.bill_to_name = "Northwind Analytics Inc"
    invoice.currency = "INR"
    assert not await matches(session, invoice)
    invoice.currency = "USD"
    invoice.invoice_date = datetime.now(UTC).date() + timedelta(days=50)
    assert not await matches(session, invoice)
    invoice.invoice_date = datetime.now(UTC).date()
    await set_status(session, row.id, activate=False, person="Finance", reason="Policy changed")
    assert not await matches(session, invoice)


async def test_reuse_category_and_gl_but_po_wins(session: AsyncSession) -> None:
    invoice, vendor = await item(session)
    for field, value in (("purchase_category", "goods"), ("gl_account", "6100")):
        row = await correction(session, invoice, field, value)
        await set_status(session, row.id, activate=True, person="Finance", reason="Validated")
    service = CodingService(session, None, suspense_account="9999", min_confidence=0.6)
    await service.code(
        invoice.lines,
        vendor=vendor,
        purchase_order=None,
        invoice_description=None,
        buyer=invoice.bill_to_name,
        currency=invoice.currency,
        invoice_date=invoice.invoice_date,
    )
    assert invoice.lines[0].gl_source == "validated_correction"
    assert invoice.lines[0].gl_account_code == "6100"
    fields = (invoice.lines[0].finance_classification or {})["fields"]
    assert fields["purchase_category"]["value"] == "goods"
    assert fields["purchase_category"]["correction_id"]
    po = (
        await session.scalars(select(PurchaseOrder).options(selectinload(PurchaseOrder.lines)))
    ).first()
    assert po is not None
    po.lines[0].finance_details = {"purchase_category": "professional_service"}
    invoice.lines[0].purchase_order_line_id = po.lines[0].id
    await service.code(
        invoice.lines,
        vendor=vendor,
        purchase_order=po,
        invoice_description=None,
        buyer=invoice.bill_to_name,
        currency=invoice.currency,
        invoice_date=invoice.invoice_date,
    )
    assert invoice.lines[0].gl_source == "po"
    assert invoice.lines[0].gl_account_code == po.lines[0].gl_account_code
    assert (invoice.lines[0].finance_classification or {})["fields"]["purchase_category"][
        "source"
    ] == "po"


async def test_tax_reuse_withdrawal_source_version_and_amount(session: AsyncSession) -> None:
    invoice, _ = await item(session)
    invoice.lines[0].description = "Printer paper cartons for office use"
    row = await correction(session, invoice, "tax_treatment", "goods")
    await set_status(
        session,
        row.id,
        activate=True,
        person="Finance",
        reason="Purchase location and source verified",
    )
    assert not await check_tax(invoice, session=session)
    review = (invoice.additional_info or {})["tax_review"]
    assert review["expected_tax"] == "6.25"
    assert review["lines"][0]["correction"]["correction_id"] == str(row.id)
    invoice.tax_total = Decimal("0")
    assert await check_tax(invoice, session=session)  # Reuse never skips arithmetic.
    row.source_version = "old-source"
    assert await check_tax(invoice, session=session)
    assert (invoice.additional_info or {})["tax_review"]["lines"][0]["expected_rate"] is None
    await set_status(session, row.id, activate=False, person="Finance", reason="Withdrawn")
    assert await check_tax(invoice, session=session)
    assert not (invoice.additional_info or {})["tax_review"]["lines"][0].get("correction")


async def test_invalid_and_overlapping_corrections(session: AsyncSession) -> None:
    invoice, _ = await item(session)
    with pytest.raises(CorrectionError, match="configured rate"):
        await correction(session, invoice, "tax_treatment", "exemption")
    with pytest.raises(CorrectionError, match="active purchase"):
        await correction(session, invoice, "gl_account", "missing")
    row = await correction(session, invoice, "purchase_category", "goods")
    await set_status(session, row.id, activate=True, person="Finance", reason="Checked")
    duplicate = await correction(session, invoice, "purchase_category", "professional_service")
    with pytest.raises(CorrectionError, match="overlapping"):
        await set_status(session, duplicate.id, activate=True, person="Finance", reason="Checked")


def test_reviewer_forms(app: FastAPI, client: TestClient, seeded_settings: Settings) -> None:
    body, _ = _process(
        app, client, seeded_settings, acme_invoice_fields(bill_to_name="Northwind Analytics Inc")
    )
    page = client.get(f"/invoices/{body['id']}")
    assert "Save a recurring purchase correction" in page.text
    today = datetime.now(UTC).date()
    response = client.post(
        f"/invoices/{body['id']}/corrections",
        data={
            "line_no": "1",
            "choice": "purchase_category:goods",
            "person": "Reviewer",
            "evidence": "Purchase description and PO confirm physical goods.",
            "valid_from": str(today),
            "valid_until": str(today + timedelta(days=30)),
        },
    )
    assert response.status_code == 200, response.text
    assert "Correction saved as pending" in response.text
    assert "Validate for future matches" in response.text
    action = re.search(r'action="(/corrections/[^/]+/validate)"', response.text)
    assert action is not None
    response = client.post(
        action.group(1), data={"person": "Finance", "reason": "Scope and evidence confirmed"}
    )
    assert response.status_code == 200
    assert "Withdraw correction" in response.text
    withdrawal = re.search(r'action="(/corrections/[^/]+/withdraw)"', response.text)
    assert withdrawal is not None
    response = client.post(
        withdrawal.group(1), data={"person": "Finance", "reason": "Policy replaced"}
    )
    assert response.status_code == 200
    assert "Withdrawn" in response.text
