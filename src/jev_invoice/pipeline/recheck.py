"""After a delivery: check the invoices waiting on that purchase order again."""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.db.models import Invoice
from jev_invoice.domain.enums import InvoiceStatus
from jev_invoice.pipeline.service import InvoicePipeline


@dataclass
class Rechecked:
    checked: list[str] = field(default_factory=list)
    approved: list[str] = field(default_factory=list)


async def recheck_waiting_on(
    session: AsyncSession,
    pipeline: InvoicePipeline,
    po_id: uuid.UUID,
    *,
    actor: str,
    why: str,
) -> Rechecked:
    """Re-check every invoice on *po_id* that waits for a person (no model is called)."""
    waiting = list(
        await session.scalars(
            select(Invoice)
            .where(
                Invoice.purchase_order_id == po_id,
                Invoice.status == InvoiceStatus.PENDING_REVIEW,
            )
            .order_by(Invoice.received_at)
        )
    )
    result = Rechecked()
    for invoice in waiting:
        label = invoice.invoice_number or invoice.original_filename
        result.checked.append(label)
        if await pipeline.recheck(invoice.id, actor=actor, why=why):
            result.approved.append(label)
    return result
