"""Bounded invoice/PO/delivery evidence for a reviewer, without guessing line matches."""

import re
from collections import Counter
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.db.models import (
    AuditEvent,
    Decision,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    PurchaseOrderLine,
)


def tax_summary(invoice: Invoice) -> dict[str, Decimal | None]:
    """Summarize the recorded assessment, including assessments saved before this view."""
    tax = (invoice.additional_info or {}).get("tax_review") or {}
    expected = tax.get("expected_tax")
    actual = tax.get("invoice_tax")
    summary: dict[str, Decimal | None] = {
        "taxable_subtotal": None,
        "difference": None,
    }
    if expected is None:
        return summary
    if actual is not None:
        summary["difference"] = Decimal(actual) - Decimal(expected)
    rates = {row["line_no"]: row["expected_rate"] for row in tax.get("lines", [])}
    if invoice.lines and all(
        line.amount is not None and rates.get(line.line_no) in ("0", "6.25")
        for line in invoice.lines
    ):
        summary["taxable_subtotal"] = sum(
            (line.amount or Decimal(0) for line in invoice.lines if rates[line.line_no] == "6.25"),
            Decimal(0),
        )
    return summary


async def comparison(
    session: AsyncSession, invoice: Invoice, decision: Decision | None
) -> dict[str, Any]:
    ids = [line.purchase_order_line_id for line in invoice.lines if line.purchase_order_line_id]
    po_lines = (
        {
            line.id: line
            for line in await session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.id.in_(ids))
            )
        }
        if ids
        else {}
    )
    receipts = (
        list(
            await session.execute(
                select(GoodsReceiptLine, GoodsReceipt)
                .join(GoodsReceipt)
                .where(GoodsReceiptLine.purchase_order_line_id.in_(ids))
            )
        )
        if ids
        else []
    )
    latest_issues = (
        select(func.max(AuditEvent.id))
        .where(
            AuditEvent.entity_type == "receiving_issue",
            AuditEvent.data["po_line_id"].as_string().in_([str(i) for i in ids]),
        )
        .group_by(AuditEvent.entity_id)
    )
    issues = (
        list(await session.scalars(select(AuditEvent).where(AuditEvent.id.in_(latest_issues))))
        if ids
        else []
    )
    findings = decision.findings if decision else []
    assigned = set()
    tax = (invoice.additional_info or {}).get("tax_review", {})
    rows = []
    descriptions = Counter(line.description for line in invoice.lines)
    for line in invoice.lines:
        relevant = []
        for index, finding in enumerate(findings):
            message = finding.get("message", "")
            if re.search(rf"\bline\s+{line.line_no}\b", message, re.I) or (
                descriptions[line.description] == 1 and f"'{line.description}'" in message
            ):
                relevant.append(finding)
                assigned.add(index)
        deliveries = [
            {
                "number": receipt.receipt_number,
                "date": receipt.received_date,
                "quantity": received.quantity_received,
            }
            for received, receipt in receipts
            if received.purchase_order_line_id == line.purchase_order_line_id
        ]
        rows.append(
            {
                "invoice_line": line,
                "po_line": po_lines.get(line.purchase_order_line_id)
                if line.purchase_order_line_id
                else None,
                "deliveries": deliveries,
                "delivery_issues": [
                    event.data
                    for event in issues
                    if event.data.get("po_line_id") == str(line.purchase_order_line_id)
                ],
                "findings": relevant,
                "tax": next(
                    (r for r in tax.get("lines", []) if r["line_no"] == line.line_no), None
                ),
            }
        )
    return {
        "rows": rows,
        "invoice_findings": [f for i, f in enumerate(findings) if i not in assigned],
    }
