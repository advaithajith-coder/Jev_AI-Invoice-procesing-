"""Turn approved invoices into an export batch, exactly once per invoice.

For each approved invoice:
- lines come from the invoice when they reconcile with its total; otherwise one summary
  line carries the net amount, so the exported bill always equals what is owed
- a discount becomes its own negative line; shipping its own line, coded to Courier and
  Postage (6800) when that account exists
- the bill is for the invoice total; a deposit already paid is applied in the accounting
  system, as with any prepayment
- US sales tax (the default) is a cost: it becomes a "Sales tax" line coded like the rest.
  Where tax is reclaimable (VAT/GST), it is instead spread across the lines in proportion
  to their amounts (remainder on the last line)
- each line uses the account it was coded to (PO, Jev, vendor default or a person);
  uncoded invoices use the PO's single account, else the vendor's default, else the
  suspense account for recoding later
- the due date follows the vendor's agreed terms (invoice date + payment terms), or the
  invoice's own due date when that is later; never an earlier date the invoice asks for

Exported invoices move to status EXPORTED and cannot be exported again. Each batch keeps
its file, so it can be downloaded again without changing anything.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jev_invoice.accounting.controls import check_accounting, posting_period
from jev_invoice.config import Settings
from jev_invoice.core import audit
from jev_invoice.core.errors import AppError, NotFoundError
from jev_invoice.db.models import ExportBatch, GLAccount, Invoice, PurchaseOrder
from jev_invoice.domain.enums import DocumentType, InvoiceStatus
from jev_invoice.domain.payment import payment_terms
from jev_invoice.export.formats import FORMATS, ExportBill, ExportLine
from jev_invoice.ingest.coverage import require_complete_document

_CENT = Decimal("0.01")
SHIPPING_ACCOUNT = "6800"  # Courier and Postage in the demo chart of accounts
_TOLERANCE = Decimal("0.02")


class ExportError(AppError):
    status_code = 422
    code = "export_failed"


@dataclass(frozen=True)
class NotReady:
    invoice: Invoice
    reason: str


def export_query() -> Select[Invoice]:
    return select(Invoice).options(
        selectinload(Invoice.lines),
        selectinload(Invoice.vendor),
        selectinload(Invoice.purchase_order).selectinload(PurchaseOrder.lines),
    )


class ExportService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def candidates(self) -> tuple[list[tuple[Invoice, ExportBill]], list[NotReady]]:
        """Approved invoices not yet exported, split into ready and not ready (with why)."""
        invoices = await self._session.scalars(
            export_query()
            .where(
                Invoice.status == InvoiceStatus.APPROVED,
                Invoice.export_batch_id.is_(None),
                # Credit notes are applied to invoices, not exported as bills.
                or_(
                    Invoice.document_type.is_(None),
                    Invoice.document_type != DocumentType.CREDIT_NOTE,
                ),
            )
            .order_by(Invoice.received_at)
        )
        names = await self._account_names()
        ready: list[tuple[Invoice, ExportBill]] = []
        not_ready: list[NotReady] = []
        for invoice in invoices:
            checks = await check_accounting(self._session, invoice)
            bill = checks[0].message if checks else self.build_bill(invoice, names)
            if isinstance(bill, str):
                not_ready.append(NotReady(invoice, bill))
            else:
                ready.append((invoice, bill))
        return ready, not_ready

    async def create_batch(
        self, format_key: str, invoice_ids: list[uuid.UUID], *, actor: str
    ) -> ExportBatch:
        export_format = FORMATS.get(format_key)
        if export_format is None:
            raise ExportError(f"Unknown export format: {format_key}")
        if not invoice_ids:
            raise ExportError("Choose at least one invoice to export.")
        if len(invoice_ids) > self._settings.export_max_invoices:
            raise ExportError(
                f"Export at most {self._settings.export_max_invoices} invoices per file."
            )

        invoices = list(
            await self._session.scalars(
                export_query()
                .where(Invoice.id.in_(invoice_ids))
                .order_by(Invoice.received_at)
                .with_for_update()  # PostgreSQL: two exports cannot claim the same invoice
            )
        )
        names = await self._account_names()
        bills: list[ExportBill] = []
        for invoice in invoices:
            checks = await check_accounting(self._session, invoice)
            if checks:
                raise ExportError(checks[0].message)
            if (
                format_key != "standard"
                and invoice.invoice_date
                and posting_period(invoice) != invoice.invoice_date.strftime("%Y-%m")
            ):
                raise ExportError(
                    "This CSV format has no separate posting-period field. Use Standard CSV "
                    "and confirm the selected period in your accounting system."
                )
            if invoice.status != InvoiceStatus.APPROVED or invoice.export_batch_id is not None:
                raise ExportError(
                    f"Invoice {invoice.invoice_number or invoice.id} is not waiting for export"
                    f" (status: {invoice.status})."
                )
            bill = self.build_bill(invoice, names)
            if isinstance(bill, str):
                raise ExportError(f"Invoice {invoice.invoice_number or invoice.id}: {bill}")
            bills.append(bill)
        if len(invoices) != len(set(invoice_ids)):
            raise ExportError("Some selected invoices no longer exist.")

        content = export_format.render(bills, self._settings)
        now = datetime.now(UTC)
        batch = ExportBatch(
            id=uuid.uuid4(),
            format=export_format.key,
            filename=f"{export_format.filename_prefix}-{now:%Y%m%d-%H%M%S}.csv",
            created_by=actor,
            invoice_count=len(invoices),
            content=content,
            sha256=hashlib.sha256(content.encode()).hexdigest(),
        )
        self._session.add(batch)
        for invoice in invoices:
            invoice.status = InvoiceStatus.EXPORTED
            invoice.export_batch_id = batch.id
            invoice.exported_at = now
            audit.record(
                self._session,
                action="invoice.exported",
                entity_type="invoice",
                entity_id=invoice.id,
                actor=actor,
                data={
                    "batch_id": str(batch.id),
                    "format": export_format.key,
                    "posting_period": posting_period(invoice),
                },
            )
        audit.record(
            self._session,
            action="export.created",
            entity_type="export_batch",
            entity_id=batch.id,
            actor=actor,
            data={"format": export_format.key, "invoices": len(invoices), "sha256": batch.sha256},
        )
        await self._session.commit()
        return batch

    async def batches(self, *, limit: int = 50) -> list[ExportBatch]:
        return list(
            await self._session.scalars(
                select(ExportBatch).order_by(ExportBatch.created_at.desc()).limit(limit)
            )
        )

    async def batch(self, batch_id: uuid.UUID) -> ExportBatch:
        batch = await self._session.scalar(
            select(ExportBatch)
            .where(ExportBatch.id == batch_id)
            .options(selectinload(ExportBatch.invoices).selectinload(Invoice.vendor))
        )
        if batch is None:
            raise NotFoundError("Export not found", details={"batch_id": str(batch_id)})
        return batch

    # ── building one bill ─────────────────────────────────────────────────────

    def build_bill(self, invoice: Invoice, account_names: dict[str, str]) -> ExportBill | str:
        """The bill to export, or a plain-language reason it cannot be exported yet."""
        try:
            require_complete_document(invoice)
        except AppError as exc:
            return exc.message
        if invoice.document_type == DocumentType.CREDIT_NOTE:
            return "a credit note: apply it to the vendor's invoice instead"
        if invoice.hold_kind:
            return "disputed with the vendor" if invoice.hold_kind == "dispute" else "on hold"
        supplier = invoice.vendor.name if invoice.vendor else invoice.vendor_name
        currency = invoice.currency or (invoice.vendor.default_currency if invoice.vendor else None)
        if not supplier:
            return "no supplier name"
        if not invoice.invoice_number:
            return "no invoice number"
        if invoice.invoice_date is None:
            return "no invoice date"
        if invoice.total is None:
            return "no total"
        if not currency:
            return "no currency"

        # Pay on the agreed terms, not an earlier date the invoice may print.
        due_date = payment_terms(
            invoice_date=invoice.invoice_date,
            printed_due=invoice.due_date,
            total=invoice.total,
            agreed_days=invoice.vendor.payment_terms_days if invoice.vendor else 30,
        ).pay_by
        tax = invoice.tax_total or Decimal(0)
        shipping = invoice.shipping or Decimal(0)
        net = invoice.total - tax - shipping
        # Each line keeps the account it was coded to; the discount, sales tax and any
        # summary line go to the invoice's main account (its largest coded line's).
        fallback = self._account_code(invoice)
        coded = [line for line in invoice.lines if line.gl_account_code and line.amount is not None]
        account = (
            max(coded, key=lambda line: line.amount or Decimal(0)).gl_account_code or fallback
            if coded
            else fallback
        )

        # description, qty, price, amount, account
        raw: list[tuple[str, Decimal, Decimal, Decimal, str]] = []
        line_amounts = [line.amount for line in invoice.lines if line.amount is not None]
        discount = invoice.discount or Decimal(0)
        if line_amounts and abs(sum(line_amounts, Decimal(0)) - discount - net) <= _TOLERANCE:
            for line in invoice.lines:
                if line.amount is None:
                    continue
                qty, price = line.quantity, line.unit_price
                # Files carry unit prices to the penny; keep qty x price only if it still
                # adds up after that rounding, so the target system's total is exact.
                if (
                    qty is None
                    or price is None
                    or (qty * price.quantize(_CENT)).quantize(_CENT) != line.amount
                ):
                    qty, price = Decimal(1), line.amount
                else:
                    price = price.quantize(_CENT)
                raw.append(
                    (line.description, qty, price, line.amount, line.gl_account_code or account)
                )
            if discount:
                raw.append(("Discount", Decimal(1), -discount, -discount, account))
        else:
            raw.append((f"Invoice {invoice.invoice_number}", Decimal(1), net, net, account))

        if tax and self._settings.export_sales_tax_as_cost:
            # US: sales tax is a cost, not a reclaimable tax, so it is a line of its own.
            raw.append(("Sales tax", Decimal(1), tax, tax, account))
            taxes = [Decimal(0)] * len(raw)
            tax = Decimal(0)
        else:
            taxes = _spread(tax, [entry[3] for entry in raw])
        lines = tuple(
            ExportLine(
                description=description,
                quantity=qty,
                unit_price=price,
                amount=amount,
                tax_amount=line_tax,
                account_code=code,
                account_name=account_names.get(code, code),
            )
            for (description, qty, price, amount, code), line_tax in zip(raw, taxes, strict=True)
        )
        if shipping:
            # Freight is its own cost, coded to Courier and Postage when that account exists.
            freight = SHIPPING_ACCOUNT if SHIPPING_ACCOUNT in account_names else account
            lines += (
                ExportLine(
                    description="Shipping and handling",
                    quantity=Decimal(1),
                    unit_price=shipping,
                    amount=shipping,
                    tax_amount=Decimal(0),
                    account_code=freight,
                    account_name=account_names.get(freight, freight),
                ),
            )
        return ExportBill(
            supplier=supplier,
            invoice_number=invoice.invoice_number,
            invoice_date=invoice.invoice_date,
            due_date=due_date,
            currency=currency,
            tax=tax,
            total=invoice.total,
            lines=lines,
            posting_period=posting_period(invoice),
        )

    def _account_code(self, invoice: Invoice) -> str:
        po = invoice.purchase_order
        if po is not None:
            codes = {line.gl_account_code for line in po.lines if line.gl_account_code}
            if len(codes) == 1:
                return codes.pop()
        if invoice.vendor is not None and invoice.vendor.default_gl_account_code:
            return invoice.vendor.default_gl_account_code
        return self._settings.export_suspense_account

    async def _account_names(self) -> dict[str, str]:
        return {
            code: name
            for code, name in await self._session.execute(select(GLAccount.code, GLAccount.name))
        }


def _spread(total: Decimal, amounts: list[Decimal]) -> list[Decimal]:
    """Split *total* across lines in proportion to their amounts; rounding goes on the last."""
    if not amounts:
        return []
    base = sum(amounts, Decimal(0))
    if total == 0 or base == 0:
        return [total if i == len(amounts) - 1 else Decimal(0) for i in range(len(amounts))]
    shares = [(total * amount / base).quantize(_CENT) for amount in amounts[:-1]]
    return [*shares, total - sum(shares, Decimal(0))]
