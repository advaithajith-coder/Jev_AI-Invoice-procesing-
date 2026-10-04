"""Vendors and purchase orders: the records invoices are checked against."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, UploadFile, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from jev_invoice.api.dependencies import get_importer, get_po_service, get_vendor_service
from jev_invoice.db.models import Vendor
from jev_invoice.domain.enums import PurchaseOrderStatus
from jev_invoice.masterdata.importing import (
    MAX_BYTES,
    PO_TEMPLATE,
    VENDOR_TEMPLATE,
    CsvImporter,
    ImportReport,
)
from jev_invoice.masterdata.purchase_orders import (
    POCreate,
    POSummary,
    PurchaseOrderService,
    ReceiptCreate,
)
from jev_invoice.masterdata.vendors import (
    BankAccountChange,
    Person,
    VendorCreate,
    VendorFields,
    VendorService,
)

router = APIRouter(prefix="/api/v1", tags=["vendors and purchase orders"])
VendorsDep = Annotated[VendorService, Depends(get_vendor_service)]
POsDep = Annotated[PurchaseOrderService, Depends(get_po_service)]
ImporterDep = Annotated[CsvImporter, Depends(get_importer)]


# ── Schemas ───────────────────────────────────────────────────────────────────


class VendorOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    default_currency: str
    payment_terms_days: int
    early_pay_discount_pct: Decimal | None
    early_pay_days: int | None
    po_required: bool
    default_gl_account_code: str | None
    email_domain: str | None
    tax_id: str | None
    bank_account_last4: str | None
    created_at: datetime


class VendorCreateIn(VendorCreate):
    created_by: Person


class VendorUpdateIn(VendorFields):
    changed_by: Person


class POLineOut(BaseModel):
    id: uuid.UUID
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    gl_account_code: str | None
    received: Decimal
    outstanding: Decimal


class POOut(BaseModel):
    id: uuid.UUID
    po_number: str
    vendor_id: uuid.UUID
    vendor_name: str
    currency: str
    status: str
    issued_date: date
    total_amount: Decimal
    received_value: Decimal
    invoiced_value: Decimal
    lines: list[POLineOut]


class POCreateIn(POCreate):
    created_by: Person


class POStatusIn(BaseModel):
    status: Literal["closed", "cancelled"]
    changed_by: Person
    note: str | None = None


class ImportReportOut(BaseModel):
    created: int
    errors: list[str]


def vendor_out(vendor: Vendor) -> VendorOut:
    return VendorOut.model_validate(vendor, from_attributes=True)


def po_out(summary: POSummary) -> POOut:
    po = summary.po
    return POOut(
        id=po.id,
        po_number=po.po_number,
        vendor_id=po.vendor_id,
        vendor_name=po.vendor.name,
        currency=po.currency,
        status=po.status,
        issued_date=po.issued_date,
        total_amount=po.total_amount,
        received_value=summary.received_value,
        invoiced_value=summary.invoiced_value,
        lines=[
            POLineOut(
                id=item.line.id,
                line_no=item.line.line_no,
                description=item.line.description,
                quantity=item.line.quantity,
                unit_price=item.line.unit_price,
                gl_account_code=item.line.gl_account_code,
                received=item.received,
                outstanding=item.outstanding,
            )
            for item in summary.lines
        ],
    )


def report_out(report: ImportReport) -> ImportReportOut:
    return ImportReportOut(created=report.created, errors=report.errors)


# ── Vendors ───────────────────────────────────────────────────────────────────


@router.get("/vendors")
async def list_vendors(vendors: VendorsDep) -> list[VendorOut]:
    return [vendor_out(vendor) for vendor in await vendors.list_all()]


@router.post("/vendors", status_code=status.HTTP_201_CREATED)
async def create_vendor(body: VendorCreateIn, vendors: VendorsDep) -> VendorOut:
    data = VendorCreate.model_validate(body.model_dump(exclude={"created_by"}))
    return vendor_out(await vendors.create(data, actor=body.created_by))


@router.get("/vendors/import-template.csv", response_class=PlainTextResponse)
async def vendor_template() -> PlainTextResponse:
    return _csv_download(VENDOR_TEMPLATE, "vendors-template.csv")


@router.post("/vendors/import")
async def import_vendors(
    file: UploadFile, imported_by: str, importer: ImporterDep
) -> ImportReportOut:
    data = await file.read(MAX_BYTES + 1)
    return report_out(await importer.import_vendors(data, actor=imported_by.strip()[:100] or "api"))


@router.get("/vendors/{vendor_id}")
async def get_vendor(vendor_id: uuid.UUID, vendors: VendorsDep) -> VendorOut:
    return vendor_out(await vendors.get(vendor_id))


@router.put("/vendors/{vendor_id}")
async def update_vendor(
    vendor_id: uuid.UUID, body: VendorUpdateIn, vendors: VendorsDep
) -> VendorOut:
    data = VendorFields.model_validate(body.model_dump(exclude={"changed_by"}))
    return vendor_out(await vendors.update(vendor_id, data, actor=body.changed_by))


@router.put("/vendors/{vendor_id}/bank-account")
async def change_bank_account(
    vendor_id: uuid.UUID, body: BankAccountChange, vendors: VendorsDep
) -> VendorOut:
    """Change where a vendor is paid. Needs who made the change and how it was verified."""
    return vendor_out(await vendors.change_bank_account(vendor_id, body))


# ── Purchase orders ───────────────────────────────────────────────────────────


@router.get("/purchase-orders")
async def list_purchase_orders(pos: POsDep) -> list[POOut]:
    return [po_out(summary) for summary in await pos.list_all()]


@router.post("/purchase-orders", status_code=status.HTTP_201_CREATED)
async def create_purchase_order(body: POCreateIn, pos: POsDep) -> POOut:
    data = POCreate.model_validate(body.model_dump(exclude={"created_by"}))
    po = await pos.create(data, actor=body.created_by)
    return po_out(await pos.get(po.id))


@router.get("/purchase-orders/import-template.csv", response_class=PlainTextResponse)
async def po_template() -> PlainTextResponse:
    return _csv_download(PO_TEMPLATE, "purchase-orders-template.csv")


@router.post("/purchase-orders/import")
async def import_purchase_orders(
    file: UploadFile, imported_by: str, importer: ImporterDep
) -> ImportReportOut:
    data = await file.read(MAX_BYTES + 1)
    return report_out(
        await importer.import_purchase_orders(data, actor=imported_by.strip()[:100] or "api")
    )


@router.get("/purchase-orders/{po_id}")
async def get_purchase_order(po_id: uuid.UUID, pos: POsDep) -> POOut:
    return po_out(await pos.get(po_id))


@router.post("/purchase-orders/{po_id}/receipts", status_code=status.HTTP_201_CREATED)
async def record_delivery(po_id: uuid.UUID, body: ReceiptCreate, pos: POsDep) -> POOut:
    await pos.record_receipt(po_id, body)
    return po_out(await pos.get(po_id))


@router.put("/purchase-orders/{po_id}/status")
async def set_po_status(po_id: uuid.UUID, body: POStatusIn, pos: POsDep) -> POOut:
    await pos.set_status(
        po_id, PurchaseOrderStatus(body.status), actor=body.changed_by, note=body.note
    )
    return po_out(await pos.get(po_id))


def _csv_download(content: str, filename: str) -> PlainTextResponse:
    return PlainTextResponse(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
