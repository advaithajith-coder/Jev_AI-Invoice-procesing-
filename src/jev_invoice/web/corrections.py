"""Propose, validate and withdraw recurring purchase corrections."""

import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from jev_invoice.api.dependencies import SessionDep
from jev_invoice.coding.corrections import CorrectionError, propose, set_status
from jev_invoice.core.errors import AppError, NotFoundError
from jev_invoice.db.models import Invoice, PurchaseCorrection, Vendor
from jev_invoice.pipeline.service import invoice_query
from jev_invoice.web.routes import templates

router = APIRouter(include_in_schema=False)


async def page(request: Request, session: SessionDep, error: str | None = None) -> Response:
    rows = (
        await session.execute(
            select(PurchaseCorrection, Vendor.name, Invoice.bill_to_name)
            .join(Vendor, Vendor.id == PurchaseCorrection.vendor_id)
            .join(Invoice, Invoice.id == PurchaseCorrection.source_invoice_id)
            .order_by(PurchaseCorrection.created_at.desc())
            .limit(200)
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "corrections.html",
        {
            "nav": "corrections",
            "rows": rows,
            "error": error,
            "today": datetime.now(UTC).date(),
        },
        status_code=422 if error else 200,
    )


@router.get("/corrections")
async def correction_page(request: Request, session: SessionDep) -> Response:
    return await page(request, session)


@router.post("/invoices/{invoice_id}/corrections")
async def correction_proposal(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    line_no: Annotated[int, Form()],
    choice: Annotated[str, Form()],
    evidence: Annotated[str, Form()],
    person: Annotated[str, Form()],
    valid_from: Annotated[date, Form()],
    valid_until: Annotated[date, Form()],
    source_section: Annotated[str, Form()] = "",
) -> Response:
    invoice = await session.scalar(invoice_query().where(Invoice.id == invoice_id))
    if not invoice:
        raise NotFoundError("Invoice not found.")
    try:
        line = next((line for line in invoice.lines if line.line_no == line_no), None)
        if line is None or ":" not in choice:
            raise CorrectionError("Choose an invoice line and a corrected value.")
        field, value = choice.split(":", 1)
        await propose(
            session,
            invoice,
            line,
            field=field,
            value=value,
            evidence=evidence,
            person=person,
            valid_from=valid_from,
            valid_until=valid_until,
            source_section=source_section or None,
        )
        await session.commit()
    except AppError as exc:
        await session.rollback()
        return await page(request, session, exc.message)
    return RedirectResponse("/corrections?saved=1", status_code=303)


@router.post("/corrections/{correction_id}/{action}")
async def correction_action(
    request: Request,
    correction_id: uuid.UUID,
    action: str,
    session: SessionDep,
    person: Annotated[str, Form()],
    reason: Annotated[str, Form()],
) -> Response:
    try:
        if action not in {"validate", "withdraw"}:
            raise CorrectionError("Choose validate or withdraw.")
        await set_status(
            session, correction_id, activate=action == "validate", person=person, reason=reason
        )
        await session.commit()
    except AppError as exc:
        await session.rollback()
        return await page(request, session, exc.message)
    return RedirectResponse("/corrections", status_code=303)
