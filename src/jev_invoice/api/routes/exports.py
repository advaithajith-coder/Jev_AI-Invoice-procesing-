"""Export approved invoices as accounting-system import files."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from jev_invoice.api.dependencies import get_export_service
from jev_invoice.db.models import ExportBatch
from jev_invoice.export.formats import FORMATS
from jev_invoice.export.service import ExportService
from jev_invoice.masterdata.vendors import Person

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])
ExportsDep = Annotated[ExportService, Depends(get_export_service)]


class ExportBatchOut(BaseModel):
    id: uuid.UUID
    format: str
    filename: str
    created_by: str
    created_at: datetime
    invoice_count: int
    sha256: str


class ExportCreateIn(BaseModel):
    format: str = Field(description=f"One of: {', '.join(FORMATS)}")
    invoice_ids: list[uuid.UUID] | None = Field(
        default=None, description="Omit to export every approved invoice that is ready"
    )
    created_by: Person


class ReadyInvoiceOut(BaseModel):
    invoice_id: uuid.UUID
    invoice_number: str | None
    supplier: str | None
    ready: bool
    reason: str | None = None


def batch_out(batch: ExportBatch) -> ExportBatchOut:
    return ExportBatchOut.model_validate(batch, from_attributes=True)


@router.get("/ready")
async def ready_to_export(exports: ExportsDep) -> list[ReadyInvoiceOut]:
    """Approved invoices that have not been exported yet, and whether each one can be."""
    ready, not_ready = await exports.candidates()
    return [
        *(
            ReadyInvoiceOut(
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                supplier=bill.supplier,
                ready=True,
            )
            for invoice, bill in ready
        ),
        *(
            ReadyInvoiceOut(
                invoice_id=item.invoice.id,
                invoice_number=item.invoice.invoice_number,
                supplier=item.invoice.vendor_name,
                ready=False,
                reason=item.reason,
            )
            for item in not_ready
        ),
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_export(body: ExportCreateIn, exports: ExportsDep) -> ExportBatchOut:
    ids = body.invoice_ids
    if ids is None:
        ready, _ = await exports.candidates()
        ids = [invoice.id for invoice, _ in ready]
    return batch_out(await exports.create_batch(body.format, ids, actor=body.created_by))


@router.get("")
async def list_exports(exports: ExportsDep) -> list[ExportBatchOut]:
    return [batch_out(batch) for batch in await exports.batches()]


@router.get("/{batch_id}/file")
async def download_export(batch_id: uuid.UUID, exports: ExportsDep) -> Response:
    batch = await exports.batch(batch_id)
    return Response(
        content=batch.content.encode("utf-8-sig"),  # BOM so Excel reads UTF-8 correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{batch.filename}"'},
    )
