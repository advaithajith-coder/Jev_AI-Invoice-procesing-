"""The receiving screen: everything on order, and the deliveries recorded against it.

/receiving               what is still to come, line by line, and recent deliveries
/receiving/{po_id}       record a delivery on one purchase order

Deliveries are what the three-way match compares invoices with: an invoice is paid only for
what was ordered and received. Recording a delivery checks the invoices waiting on that
order again, with no model calls.
"""

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from jev_invoice.api.dependencies import SessionDep, get_pipeline
from jev_invoice.core.errors import AppError
from jev_invoice.db.models import GoodsReceipt, Invoice, PurchaseOrder
from jev_invoice.domain.enums import InvoiceStatus, PurchaseOrderStatus
from jev_invoice.masterdata.purchase_orders import ReceiptCreate, today_utc
from jev_invoice.pipeline.recheck import Rechecked, recheck_waiting_on
from jev_invoice.pipeline.service import InvoicePipeline
from jev_invoice.web.admin import POsDep, _message, _person, _redirect, _render
from jev_invoice.web.routes import FormError

router = APIRouter(include_in_schema=False)

_OPEN = (PurchaseOrderStatus.OPEN, PurchaseOrderStatus.PARTIALLY_RECEIVED)


def received_quantities(form: Any) -> dict[uuid.UUID, Decimal]:
    """`qty_<PO line id>` fields from a delivery form; empty boxes are skipped."""
    quantities: dict[uuid.UUID, Decimal] = {}
    for key, value in form.items():
        if key.startswith("qty_") and str(value).strip():
            try:
                quantities[uuid.UUID(key[4:])] = Decimal(str(value).replace(",", ""))
            except (ValueError, InvalidOperation) as exc:
                raise FormError("Received quantities must be numbers.") from exc
    return quantities


def recheck_notice(rechecked: Rechecked) -> str:
    """Query string describing what the re-check did, for the page after the redirect."""
    return urlencode(
        {
            "notice": "delivery",
            "checked": ",".join(rechecked.checked),
            "approved": ",".join(rechecked.approved),
        }
    )


async def _context(request: Request, pos: POsDep, session: SessionDep) -> dict[str, Any]:
    summaries = [
        s
        for s in await pos.list_all()
        if s.po.status in _OPEN and any(item.outstanding > 0 for item in s.lines)
    ]
    waiting = list(
        await session.scalars(
            select(Invoice)
            .where(
                Invoice.status == InvoiceStatus.PENDING_REVIEW,
                Invoice.purchase_order_id.in_([s.po.id for s in summaries]),
            )
            .options(selectinload(Invoice.decisions))
        )
    )
    deliveries = list(
        await session.scalars(
            select(GoodsReceipt)
            .options(
                selectinload(GoodsReceipt.lines),
                selectinload(GoodsReceipt.purchase_order).selectinload(PurchaseOrder.vendor),
                selectinload(GoodsReceipt.purchase_order).selectinload(PurchaseOrder.lines),
            )
            .order_by(GoodsReceipt.received_date.desc(), GoodsReceipt.created_at.desc())
            .limit(20)
        )
    )
    params = request.query_params
    return {
        "orders": [(s, [i for i in waiting if i.purchase_order_id == s.po.id]) for s in summaries],
        "deliveries": deliveries,
        "today": today_utc(),
        "checked": [x for x in params.get("checked", "").split(",") if x],
        "approved": [x for x in params.get("approved", "").split(",") if x],
        "nav": "receiving",
    }


@router.get("/receiving", response_class=HTMLResponse)
async def receiving_page(request: Request, session: SessionDep, pos: POsDep) -> HTMLResponse:
    return _render(request, "receiving.html", await _context(request, pos, session))


@router.post("/receiving/{po_id}", response_model=None)
async def record_delivery(
    request: Request,
    po_id: uuid.UUID,
    session: SessionDep,
    pos: POsDep,
    pipeline: InvoicePipeline = Depends(get_pipeline),  # noqa: B008 - FastAPI dependency
) -> Response:
    form = await request.form()
    person = str(form.get("person", ""))
    try:
        received = str(form.get("received_date", "")) or today_utc().isoformat()
        data = ReceiptCreate(
            received_date=date.fromisoformat(received),
            received_by=_person(person),
            quantities=received_quantities(form),
        )
        receipt = await pos.record_receipt(po_id, data)
    except (AppError, ValidationError, ValueError) as exc:
        await session.rollback()
        context = await _context(request, pos, session)
        context.update(error=_message(exc), person=person, error_po=po_id)
        return _render(request, "receiving.html", context, 400)
    rechecked = await recheck_waiting_on(
        session, pipeline, po_id, actor=data.received_by, why=f"delivery {receipt.receipt_number}"
    )
    return _redirect(f"/receiving?{recheck_notice(rechecked)}", person.strip())
