"""Purchase orders and deliveries (goods receipts).

A PO says what was ordered; a goods receipt says what actually arrived. Invoices are
matched against both (2-way: billed <= ordered, 3-way: billed <= received), so
recording deliveries here is what lets clean invoices be approved automatically.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jev_invoice.core import audit
from jev_invoice.core.errors import ConflictError, NotFoundError
from jev_invoice.db.models import (
    GLAccount,
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from jev_invoice.domain.enums import InvoiceStatus, PurchaseOrderStatus
from jev_invoice.masterdata.vendors import InvalidInputError, Person
from jev_invoice.matching.service import normalize_reference

PONumber = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=2,
        max_length=50,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9\-/ .]*$",
    ),
]
_CENT = Decimal("0.01")
_CLOSED = (PurchaseOrderStatus.CLOSED, PurchaseOrderStatus.CANCELLED)
_INACTIVE_INVOICES = (InvoiceStatus.REJECTED, InvoiceStatus.FAILED)


class POLineIn(BaseModel):
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    gl_account_code: str | None = None


class POCreate(BaseModel):
    po_number: PONumber
    vendor_id: uuid.UUID
    issued_date: date
    currency: str | None = None
    lines: list[POLineIn] = Field(min_length=1, max_length=200)
    # Goods: invoices are also matched to deliveries. Services: matched to the order only.
    needs_receiving: bool = True

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        if not value:
            return None
        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError("currency must be a 3-letter ISO code such as USD")
        return code


class ReceiptCreate(BaseModel):
    received_date: date
    received_by: Person
    quantities: dict[uuid.UUID, Decimal] = Field(description="PO line id -> quantity received")

    @field_validator("quantities")
    @classmethod
    def _positive(cls, value: dict[uuid.UUID, Decimal]) -> dict[uuid.UUID, Decimal]:
        cleaned = {line: qty for line, qty in value.items() if qty != 0}
        if not cleaned:
            raise ValueError("enter a received quantity for at least one line")
        if any(qty < 0 for qty in cleaned.values()):
            raise ValueError("received quantities cannot be negative")
        return cleaned


@dataclass(frozen=True)
class POLineSummary:
    line: PurchaseOrderLine
    received: Decimal

    @property
    def outstanding(self) -> Decimal:
        return max(self.line.quantity - self.received, Decimal(0))


@dataclass(frozen=True)
class POSummary:
    po: PurchaseOrder
    lines: list[POLineSummary]
    received_value: Decimal
    invoiced_value: Decimal


def po_query() -> Select[PurchaseOrder]:
    return select(PurchaseOrder).options(
        selectinload(PurchaseOrder.vendor),
        selectinload(PurchaseOrder.lines),
        selectinload(PurchaseOrder.receipts).selectinload(GoodsReceipt.lines),
    )


class PurchaseOrderService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> list[POSummary]:
        pos = await self._session.scalars(po_query().order_by(PurchaseOrder.issued_date.desc()))
        invoiced = await self._invoiced_by_po()
        return [self._summarise(po, invoiced.get(po.id, Decimal(0))) for po in pos]

    async def get(self, po_id: uuid.UUID) -> POSummary:
        self._session.expire_all()
        po = await self._session.scalar(po_query().where(PurchaseOrder.id == po_id))
        if po is None:
            raise NotFoundError("Purchase order not found", details={"po_id": str(po_id)})
        invoiced = await self._invoiced_by_po(po.id)
        return self._summarise(po, invoiced.get(po.id, Decimal(0)))

    async def invoices_for(self, po_id: uuid.UUID) -> list[Invoice]:
        return list(
            await self._session.scalars(
                select(Invoice)
                .where(Invoice.purchase_order_id == po_id)
                .order_by(Invoice.received_at.desc())
            )
        )

    async def create(self, data: POCreate, *, actor: str, commit: bool = True) -> PurchaseOrder:
        vendor = await self._session.get(Vendor, data.vendor_id)
        if vendor is None:
            raise InvalidInputError("Choose an existing vendor.")
        await self._check_number_free(data.po_number)
        lines = []
        for number, line in enumerate(data.lines, start=1):
            gl = line.gl_account_code or vendor.default_gl_account_code
            if gl and await self._session.get(GLAccount, gl) is None:
                raise InvalidInputError(f"Line {number}: GL account {gl} does not exist.")
            lines.append(
                PurchaseOrderLine(
                    id=uuid.uuid4(),
                    line_no=number,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    gl_account_code=gl,
                )
            )
        po = PurchaseOrder(
            id=uuid.uuid4(),
            po_number=data.po_number.upper(),
            vendor_id=vendor.id,
            currency=data.currency or vendor.default_currency,
            status=PurchaseOrderStatus.OPEN,
            issued_date=data.issued_date,
            needs_receiving=data.needs_receiving,
            total_amount=sum(
                ((line.quantity * line.unit_price).quantize(_CENT) for line in lines), Decimal(0)
            ),
            lines=lines,
            receipts=[],
        )
        self._session.add(po)
        audit.record(
            self._session,
            action="po.created",
            entity_type="purchase_order",
            entity_id=po.id,
            actor=actor,
            data={
                "po_number": po.po_number,
                "vendor": vendor.name,
                "total": str(po.total_amount),
                "needs_receiving": po.needs_receiving,
            },
        )
        if commit:
            await self._session.commit()
        return po

    async def set_needs_receiving(self, po_id: uuid.UUID, needs: bool, *, actor: str) -> None:
        """Goods (matched to deliveries) or services (matched to the order only)."""
        summary = await self.get(po_id)
        po = summary.po
        if po.needs_receiving == needs:
            return
        po.needs_receiving = needs
        audit.record(
            self._session,
            action="po.receiving_changed",
            entity_type="purchase_order",
            entity_id=po.id,
            actor=actor,
            data={"needs_receiving": needs},
        )
        await self._session.commit()

    async def record_receipt(self, po_id: uuid.UUID, data: ReceiptCreate) -> GoodsReceipt:
        summary = await self.get(po_id)
        po = summary.po
        if po.status in _CLOSED:
            raise ConflictError(f"{po.po_number} is {po.status}; deliveries cannot be recorded.")
        if not po.needs_receiving:
            raise ConflictError(
                f"{po.po_number} is matched to the order only (services); nothing is received."
            )
        by_id = {item.line.id: item for item in summary.lines}
        for line_id, quantity in data.quantities.items():
            item = by_id.get(line_id)
            if item is None:
                raise InvalidInputError("A received line does not belong to this purchase order.")
            if quantity > item.outstanding:
                raise InvalidInputError(
                    f"Line {item.line.line_no}: received {_qty(quantity)} but only"
                    f" {_qty(item.outstanding)} is still outstanding."
                )
        receipt = GoodsReceipt(
            id=uuid.uuid4(),
            receipt_number=f"GRN-{uuid.uuid4().hex[:8].upper()}",
            purchase_order_id=po.id,
            received_date=data.received_date,
            lines=[
                GoodsReceiptLine(
                    id=uuid.uuid4(), purchase_order_line_id=line_id, quantity_received=qty
                )
                for line_id, qty in data.quantities.items()
            ],
        )
        self._session.add(receipt)
        fully_received = all(
            item.received + data.quantities.get(item.line.id, Decimal(0)) >= item.line.quantity
            for item in summary.lines
        )
        po.status = (
            PurchaseOrderStatus.RECEIVED
            if fully_received
            else PurchaseOrderStatus.PARTIALLY_RECEIVED
        )
        audit.record(
            self._session,
            action="po.delivery_recorded",
            entity_type="purchase_order",
            entity_id=po.id,
            actor=data.received_by,
            data={
                "receipt_number": receipt.receipt_number,
                "received_date": data.received_date.isoformat(),
                "lines": {
                    str(by_id[line_id].line.line_no): str(qty)
                    for line_id, qty in data.quantities.items()
                },
            },
        )
        await self._session.commit()
        return receipt

    async def set_status(
        self, po_id: uuid.UUID, status: PurchaseOrderStatus, *, actor: str, note: str | None
    ) -> PurchaseOrder:
        if status not in _CLOSED:
            raise InvalidInputError("Purchase orders can only be closed or cancelled by hand.")
        summary = await self.get(po_id)
        po = summary.po
        if po.status in _CLOSED:
            raise ConflictError(f"{po.po_number} is already {po.status}.")
        if status == PurchaseOrderStatus.CANCELLED and summary.received_value > 0:
            raise ConflictError(
                f"{po.po_number} has deliveries recorded; close it instead of cancelling."
            )
        po.status = status
        audit.record(
            self._session,
            action=f"po.{status}",
            entity_type="purchase_order",
            entity_id=po.id,
            actor=actor,
            data={"note": note},
        )
        await self._session.commit()
        return po

    # ── helpers ───────────────────────────────────────────────────────────────

    def _summarise(self, po: PurchaseOrder, invoiced: Decimal) -> POSummary:
        received: dict[uuid.UUID, Decimal] = {}
        for receipt in po.receipts:
            for line in receipt.lines:
                received[line.purchase_order_line_id] = (
                    received.get(line.purchase_order_line_id, Decimal(0)) + line.quantity_received
                )
        lines = [POLineSummary(line, received.get(line.id, Decimal(0))) for line in po.lines]
        received_value = sum(
            ((item.received * item.line.unit_price).quantize(_CENT) for item in lines), Decimal(0)
        )
        return POSummary(po=po, lines=lines, received_value=received_value, invoiced_value=invoiced)

    async def _invoiced_by_po(self, po_id: uuid.UUID | None = None) -> dict[uuid.UUID, Decimal]:
        query = select(
            Invoice.purchase_order_id,
            Invoice.subtotal,
            Invoice.discount,
            Invoice.total,
            Invoice.tax_total,
        ).where(Invoice.purchase_order_id.is_not(None), Invoice.status.not_in(_INACTIVE_INVOICES))
        if po_id is not None:
            query = query.where(Invoice.purchase_order_id == po_id)
        totals: dict[uuid.UUID, Decimal] = {}
        for po, subtotal, discount, total, tax in await self._session.execute(query):
            if po is None:
                continue
            if subtotal is not None:
                net = subtotal - (discount or 0)
            elif total is not None:
                net = total - (tax or 0)
            else:
                continue
            totals[po] = totals.get(po, Decimal(0)) + net
        return totals

    async def _check_number_free(self, po_number: str) -> None:
        clash = await self._session.scalar(
            select(PurchaseOrder.po_number).where(
                func.replace(func.upper(PurchaseOrder.po_number), "-", "")
                == normalize_reference(po_number)
            )
        )
        if clash is not None:
            raise ConflictError(f"Purchase order {clash} already exists.")


def _qty(value: Decimal) -> str:
    """2.0000 -> '2', 2.5000 -> '2.5'."""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def today_utc() -> date:
    return datetime.now(UTC).date()
