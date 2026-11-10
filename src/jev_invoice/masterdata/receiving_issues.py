"""Delivery discrepancies, persisted as append-only issue events in the audit trail."""

import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.core import audit
from jev_invoice.core.errors import NotFoundError
from jev_invoice.db.models import AuditEvent
from jev_invoice.domain.enums import PurchaseOrderStatus
from jev_invoice.masterdata.purchase_orders import PurchaseOrderService
from jev_invoice.masterdata.vendors import InvalidInputError
from jev_invoice.payables.service import PayablesService, is_credit_note

ISSUE_REASONS = {
    "not_received": "Missing / short delivery",
    "damaged": "Damaged goods",
    "wrong_item": "Wrong item delivered",
    "returned": "Goods returned to supplier",
}


class ReceivingIssueService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[dict[str, Any]]:
        latest = (
            select(func.max(AuditEvent.id))
            .where(AuditEvent.entity_type == "receiving_issue")
            .group_by(AuditEvent.entity_id)
        )
        events = await self.session.scalars(
            select(AuditEvent).where(AuditEvent.id.in_(latest)).order_by(AuditEvent.id.desc())
        )
        return [dict(event.data) for event in events]

    async def get(self, issue_id: uuid.UUID) -> dict[str, Any]:
        event = await self.session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == "receiving_issue",
                AuditEvent.entity_id == str(issue_id),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        if event is None:
            raise NotFoundError("Delivery issue not found.")
        return dict(event.data)

    def record(self, issue: dict[str, Any], action: str, actor: str) -> None:
        audit.record(
            self.session,
            action=action,
            entity_type="receiving_issue",
            entity_id=issue["id"],
            actor=actor,
            data=issue,
        )

    async def report(
        self,
        po_id: uuid.UUID,
        *,
        line_id: uuid.UUID,
        quantity: str,
        category: str,
        reason: str,
        actor: str,
    ) -> dict[str, Any]:
        summary = await PurchaseOrderService(self.session).get(po_id)
        if not summary.po.needs_receiving:
            raise InvalidInputError("Service orders do not have delivery issues.")
        if summary.po.status == PurchaseOrderStatus.CANCELLED:
            raise InvalidInputError("Cancelled orders cannot have new delivery issues.")
        if category not in ISSUE_REASONS:
            raise InvalidInputError("Choose the delivery issue.")
        reason = reason.strip()
        if not reason or len(reason) > 2000:
            raise InvalidInputError("Describe the issue in 1 to 2000 characters.")
        try:
            qty = Decimal(quantity)
        except InvalidOperation as exc:
            raise InvalidInputError("Affected quantity must be a number.") from exc
        item = next((i for i in summary.lines if i.line.id == line_id), None)
        if item is None:
            raise InvalidInputError("The item does not belong to this order.")
        available = item.outstanding if category == "not_received" else item.received
        if not qty.is_finite() or qty <= 0 or qty > available:
            raise InvalidInputError(
                f"Affected quantity must be positive and no greater than {available}."
            )
        active = [
            i
            for i in await self.list_all()
            if i["po_line_id"] == str(line_id) and i["status"] != "credit_applied"
        ]
        if active:
            raise InvalidInputError("This item already has an open delivery issue.")
        issue = {
            "id": str(uuid.uuid4()),
            "po_id": str(po_id),
            "po_number": summary.po.po_number,
            "vendor": summary.po.vendor.name,
            "currency": summary.po.currency,
            "po_line_id": str(line_id),
            "description": item.line.description,
            "quantity": str(qty),
            "category": category,
            "reason": reason,
            "estimated_credit": str((qty * item.line.unit_price).quantize(Decimal(".01"))),
            "status": "awaiting_credit",
            "invoice_id": None,
            "invoice_number": None,
            "credit_number": None,
            "credit_amount": None,
        }
        self.record(issue, "receiving.issue_reported", actor)
        await self.session.commit()
        return issue

    async def dispute(
        self,
        issue_id: uuid.UUID,
        invoice_id: uuid.UUID,
        *,
        actor: str,
    ) -> None:
        issue = await self.get(issue_id)
        if issue["status"] == "credit_applied":
            raise InvalidInputError("A supplier credit has already been applied for this issue.")
        service = PayablesService(self.session)
        invoice = await service.get(invoice_id)
        if invoice.purchase_order_id != uuid.UUID(issue["po_id"]) or is_credit_note(invoice):
            raise InvalidInputError("Choose an invoice for this purchase order.")
        if invoice.hold_kind:
            raise InvalidInputError("That invoice is already on hold; review its existing hold.")
        if issue["invoice_id"] and issue["invoice_id"] != str(invoice_id):
            raise InvalidInputError("This issue is already linked to another invoice.")
        issue.update(invoice_id=str(invoice_id), invoice_number=invoice.invoice_number)
        self.record(issue, "receiving.issue_disputed", actor)
        await service.hold(
            invoice_id,
            kind="dispute",
            category=issue["category"]
            if issue["category"] in ("damaged", "not_received")
            else "other",
            reason=(
                f"{ISSUE_REASONS[issue['category']]}: {issue['description']} "
                f"({issue['quantity']}). {issue['reason']}"
            ),
            actor=actor,
            commit=False,
        )

        await self.session.commit()

    async def apply_credit(
        self,
        issue_id: uuid.UUID,
        invoice_id: uuid.UUID,
        *,
        actor: str,
        credit_note_id: uuid.UUID | None,
        number: str,
        amount: str,
    ) -> None:
        issue = await self.get(issue_id)
        if issue["status"] == "credit_applied":
            raise InvalidInputError("A credit has already been applied for this delivery issue.")
        service = PayablesService(self.session)
        invoice = await service.get(invoice_id)
        if invoice.purchase_order_id != uuid.UUID(issue["po_id"]) or is_credit_note(invoice):
            raise InvalidInputError("Choose an invoice for this purchase order.")
        if issue["invoice_id"] and issue["invoice_id"] != str(invoice_id):
            raise InvalidInputError("Apply the credit to the invoice linked to this issue.")
        try:
            value = Decimal(amount) if credit_note_id is None else None
        except InvalidOperation as exc:
            raise InvalidInputError("Credit amount must be a number.") from exc
        if value is not None and not value.is_finite():
            raise InvalidInputError("Credit amount must be a finite number.")
        credit = await service.apply_credit(
            invoice_id,
            actor=actor,
            credit_note_id=credit_note_id,
            number=number,
            amount=value,
            reason=issue["reason"],
            commit=False,
        )
        issue.update(
            status="credit_applied",
            invoice_id=str(invoice_id),
            invoice_number=invoice.invoice_number,
            credit_number=credit.number,
            credit_amount=str(credit.amount),
        )
        self.record(issue, "receiving.issue_credited", actor)
        await self.session.commit()
