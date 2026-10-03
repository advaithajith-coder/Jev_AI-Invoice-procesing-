"""Invoice upload and retrieval."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, UploadFile, status

from jev_invoice.api.dependencies import SessionDep, get_app_settings, get_pipeline
from jev_invoice.api.schemas import InvoiceOut, invoice_out
from jev_invoice.config import Settings
from jev_invoice.core.errors import AppError, NotFoundError
from jev_invoice.db.models import Invoice
from jev_invoice.domain.enums import InvoiceStatus
from jev_invoice.pipeline.service import InvoicePipeline, invoice_query

router = APIRouter(prefix="/api/v1/invoices", tags=["invoices"])


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "This exact file was uploaded before"}},
)
async def upload_invoice(
    file: UploadFile,
    response: Response,
    session: SessionDep,
    pipeline: Annotated[InvoicePipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> InvoiceOut:
    """Upload a PDF, .txt, or .eml invoice and run the full pipeline on it."""
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"File is larger than {settings.max_upload_bytes // (1024 * 1024)} MB."
        )
    result = await pipeline.process_upload(data, file.filename or "upload", file.content_type)
    if result.already_received:
        response.status_code = status.HTTP_200_OK
    return await _load(session, result.invoice_id, already_received=result.already_received)


@router.get("")
async def list_invoices(
    session: SessionDep,
    status_filter: Annotated[InvoiceStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[InvoiceOut]:
    query = invoice_query().order_by(Invoice.received_at.desc()).limit(limit)
    if status_filter is not None:
        query = query.where(Invoice.status == status_filter)
    return [invoice_out(invoice) for invoice in await session.scalars(query)]


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: uuid.UUID, session: SessionDep) -> InvoiceOut:
    return await _load(session, invoice_id)


async def _load(
    session: SessionDep, invoice_id: uuid.UUID, *, already_received: bool = False
) -> InvoiceOut:
    session.expire_all()
    invoice = await session.scalar(invoice_query().where(Invoice.id == invoice_id))
    if invoice is None:
        raise NotFoundError("Invoice not found", details={"invoice_id": str(invoice_id)})
    return invoice_out(invoice, already_received=already_received)
