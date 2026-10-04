"""CSV import for vendors and purchase orders.

All-or-nothing: every row is validated first, and if any row has a problem nothing is
saved and every problem is reported with its row number. Existing vendors are never
changed by an import (that would make bank-detail changes possible without a note);
edit them on the vendor page instead.
"""

import csv
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import partial

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.core.errors import AppError
from jev_invoice.db.models import Vendor
from jev_invoice.domain.enums import VendorStatus
from jev_invoice.domain.vendors import normalize_vendor_name
from jev_invoice.masterdata.purchase_orders import POCreate, POLineIn, PurchaseOrderService
from jev_invoice.masterdata.vendors import VendorCreate, VendorService

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000

VENDOR_COLUMNS = (
    "name",
    "currency",
    "payment_terms_days",
    "default_gl_account",
    "bank_account",
    "email_domain",
    "tax_id",
    "status",
    "po_required",
    "early_pay_discount_pct",
    "early_pay_days",
)
VENDOR_REQUIRED = {"name", "currency", "default_gl_account"}
VENDOR_TEMPLATE = (
    ",".join(VENDOR_COLUMNS)
    + "\nNorthgate Cleaning LLC,USD,30,6400,Routing 021000021 Account 5555555566,"
    + "northgatecleaning.com,12-3123123,active,no,2,10\n"
)

PO_COLUMNS = (
    "po_number",
    "vendor",
    "issued_date",
    "currency",
    "description",
    "quantity",
    "unit_price",
    "gl_account",
)
PO_REQUIRED = {"po_number", "vendor", "issued_date", "description", "quantity", "unit_price"}
PO_TEMPLATE = (
    ",".join(PO_COLUMNS)
    + "\nPO-2001,Acme Supplies Inc,2026-10-01,USD,Copier paper case of 10 reams,20,24.99,6000"
    + "\nPO-2001,Acme Supplies Inc,2026-10-01,USD,Toner cartridge black,4,89.50,6000\n"
)


@dataclass
class ImportReport:
    created: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class CsvImporter:
    def __init__(
        self, session: AsyncSession, vendors: VendorService, purchase_orders: PurchaseOrderService
    ) -> None:
        self._session = session
        self._vendors = vendors
        self._pos = purchase_orders

    async def import_vendors(self, data: bytes, *, actor: str) -> ImportReport:
        report = ImportReport()
        rows = _read_csv(data, VENDOR_REQUIRED, report)
        existing = set(await self._session.scalars(select(Vendor.normalized_name)))
        seen: dict[str, int] = {}
        valid: list[VendorCreate] = []
        for number, row in rows:
            try:
                vendor = VendorCreate(
                    name=row.get("name", ""),
                    default_currency=row.get("currency") or "USD",
                    payment_terms_days=int(row.get("payment_terms_days") or 30),
                    default_gl_account_code=row.get("default_gl_account", ""),
                    bank_account=row.get("bank_account") or None,
                    email_domain=row.get("email_domain") or None,
                    tax_id=row.get("tax_id") or None,
                    status=VendorStatus((row.get("status") or "active").lower()),
                    po_required=_yes_no(row.get("po_required"), default=True),
                    early_pay_discount_pct=(
                        _decimal(row["early_pay_discount_pct"], "early_pay_discount_pct")
                        if row.get("early_pay_discount_pct")
                        else None
                    ),
                    early_pay_days=int(row["early_pay_days"])
                    if row.get("early_pay_days")
                    else None,
                )
            except (ValidationError, ValueError) as exc:
                report.errors.append(f"Row {number}: {_explain(exc)}")
                continue
            key = normalize_vendor_name(vendor.name)
            if key in existing:
                report.errors.append(
                    f"Row {number}: {vendor.name} already exists. Edit it on its vendor page."
                )
            elif key in seen:
                report.errors.append(
                    f"Row {number}: {vendor.name} also appears on row {seen[key]}."
                )
            else:
                seen[key] = number
                valid.append(vendor)
        if report.errors:
            return report
        return await self._save(
            report, [partial(self._vendors.create, v, actor=actor, commit=False) for v in valid]
        )

    async def import_purchase_orders(self, data: bytes, *, actor: str) -> ImportReport:
        report = ImportReport()
        rows = _read_csv(data, PO_REQUIRED, report)
        vendors = {
            vendor.normalized_name: vendor for vendor in await self._session.scalars(select(Vendor))
        }
        grouped: dict[str, list[tuple[int, dict[str, str]]]] = {}
        for number, row in rows:
            grouped.setdefault(row.get("po_number", "").strip().upper(), []).append((number, row))

        valid: list[POCreate] = []
        for po_number, po_rows in grouped.items():
            first_number, first = po_rows[0]
            vendor = vendors.get(normalize_vendor_name(first.get("vendor", "")))
            if vendor is None:
                report.errors.append(
                    f"Row {first_number}: vendor '{first.get('vendor', '')}'"
                    " is not in the vendor list."
                )
                continue
            try:
                lines = [
                    POLineIn(
                        description=row.get("description", ""),
                        quantity=_decimal(row.get("quantity"), "quantity"),
                        unit_price=_decimal(row.get("unit_price"), "unit_price"),
                        gl_account_code=row.get("gl_account") or None,
                    )
                    for _, row in po_rows
                ]
                valid.append(
                    POCreate(
                        po_number=po_number,
                        vendor_id=vendor.id,
                        issued_date=date.fromisoformat(first.get("issued_date", "").strip()),
                        currency=first.get("currency") or None,
                        lines=lines,
                    )
                )
            except (ValidationError, ValueError) as exc:
                rows_label = ", ".join(str(n) for n, _ in po_rows)
                report.errors.append(f"Row {rows_label} ({po_number}): {_explain(exc)}")
        if report.errors:
            return report
        return await self._save(
            report, [partial(self._pos.create, po, actor=actor, commit=False) for po in valid]
        )

    async def _save(
        self, report: ImportReport, steps: list[Callable[[], Awaitable[object]]]
    ) -> ImportReport:
        try:
            for step in steps:
                await step()
                report.created += 1
            await self._session.commit()
        except AppError as exc:
            await self._session.rollback()
            report.created = 0
            report.errors.append(exc.message)
        return report


def _read_csv(
    data: bytes, required: set[str], report: ImportReport
) -> list[tuple[int, dict[str, str]]]:
    if len(data) > MAX_BYTES:
        report.errors.append("The file is larger than 2 MB. Split it into smaller files.")
        return []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        report.errors.append("The file is not UTF-8 text. Save it as 'CSV UTF-8' and try again.")
        return []
    reader = csv.DictReader(io.StringIO(text))
    headers = {(name or "").strip().lstrip("*").lower() for name in reader.fieldnames or []}
    missing = required - headers
    if missing:
        report.errors.append(f"Missing columns: {', '.join(sorted(missing))}.")
        return []
    rows: list[tuple[int, dict[str, str]]] = []
    for number, raw in enumerate(reader, start=2):  # row 1 is the header
        row = {(k or "").strip().lstrip("*").lower(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue
        if len(rows) >= MAX_ROWS:
            report.errors.append(f"More than {MAX_ROWS} rows. Split the file.")
            break
        rows.append((number, row))
    if not rows and not report.errors:
        report.errors.append("The file has no data rows.")
    return rows


def _decimal(value: str | None, name: str) -> Decimal:
    try:
        return Decimal((value or "").replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"{name} '{value}' is not a number") from exc


def _yes_no(value: str | None, *, default: bool) -> bool:
    if not value:
        return default
    text = value.strip().lower()
    if text in ("yes", "y", "true", "1"):
        return True
    if text in ("no", "n", "false", "0"):
        return False
    raise ValueError(f"'{value}' should be yes or no")


def _explain(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
    return str(exc)
