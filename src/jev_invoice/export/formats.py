"""Import-file formats for accounting systems.

Each bill becomes one row per line, with the bill's own fields repeated on every row,
which is how both Xero and QuickBooks Online import multi-line bills.

- Xero bills import: *ContactName, *InvoiceNumber, *InvoiceDate, *DueDate, Description,
  *Quantity, *UnitAmount, *AccountCode, *TaxType, Currency. Dates are written as
  15-Mar-2026, which Xero reads without day/month ambiguity. Unit amounts exclude tax.
- QuickBooks Online bills import: Bill no., Supplier, Bill Date, Due Date, Account, Line
  Description, Line Amount, Line Tax Code, Line Tax Amount, Currency Code. The tax amount
  is given explicitly so QuickBooks uses it instead of recalculating.
- Standard CSV: every field, for spreadsheets or any other system.

Text that came from an invoice is attacker-controlled. Spreadsheet programs run cells
starting with = + - @ as formulas, so such text is prefixed with an apostrophe.
"""

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from jev_invoice.config import Settings

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class ExportLine:
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    tax_amount: Decimal
    account_code: str
    account_name: str


@dataclass(frozen=True)
class ExportBill:
    supplier: str
    invoice_number: str
    invoice_date: date
    due_date: date
    currency: str
    tax: Decimal
    total: Decimal
    lines: tuple[ExportLine, ...]
    posting_period: str | None = None


@dataclass(frozen=True)
class ExportFormat:
    key: str
    label: str
    filename_prefix: str
    render: Callable[[list[ExportBill], Settings], str]


def safe_text(value: str) -> str:
    """Neutralise text that a spreadsheet would execute as a formula."""
    return f"'{value}" if value.startswith(_FORMULA_START) else value


def _write(header: list[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _quantity(value: Decimal) -> str:
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def render_xero(bills: list[ExportBill], settings: Settings) -> str:
    header = [
        "*ContactName", "*InvoiceNumber", "*InvoiceDate", "*DueDate", "Description",
        "*Quantity", "*UnitAmount", "*AccountCode", "*TaxType", "Currency",
    ]  # fmt: skip
    rows = []
    for bill in bills:
        tax_type = settings.xero_tax_type_taxed if bill.tax > 0 else settings.xero_tax_type_untaxed
        for line in bill.lines:
            rows.append(
                [
                    safe_text(bill.supplier),
                    safe_text(bill.invoice_number),
                    bill.invoice_date.strftime("%d-%b-%Y"),
                    bill.due_date.strftime("%d-%b-%Y"),
                    safe_text(line.description),
                    _quantity(line.quantity),
                    _money(line.unit_price),
                    line.account_code,
                    tax_type,
                    bill.currency,
                ]
            )
    return _write(header, rows)


def render_quickbooks(bills: list[ExportBill], settings: Settings) -> str:
    header = [
        "Bill no.", "Supplier", "Bill Date", "Due Date", "Account", "Line Description",
        "Line Amount", "Line Tax Code", "Line Tax Amount", "Currency Code",
    ]  # fmt: skip
    rows = []
    for bill in bills:
        for line in bill.lines:
            taxed = line.tax_amount != 0
            rows.append(
                [
                    safe_text(bill.invoice_number),
                    safe_text(bill.supplier),
                    bill.invoice_date.strftime(settings.quickbooks_date_format),
                    bill.due_date.strftime(settings.quickbooks_date_format),
                    line.account_name,
                    safe_text(line.description),
                    _money(line.amount),
                    settings.quickbooks_tax_code_taxed
                    if taxed
                    else settings.quickbooks_tax_code_untaxed,
                    _money(line.tax_amount),
                    bill.currency,
                ]
            )
    return _write(header, rows)


def render_standard(bills: list[ExportBill], settings: Settings) -> str:
    header = [
        "supplier", "invoice_number", "invoice_date", "due_date", "currency",
        "line_description", "quantity", "unit_price", "line_amount", "line_tax",
        "account_code", "account_name", "invoice_tax", "invoice_total", "posting_period",
    ]  # fmt: skip
    rows = [
        [
            safe_text(bill.supplier),
            safe_text(bill.invoice_number),
            bill.invoice_date.isoformat(),
            bill.due_date.isoformat(),
            bill.currency,
            safe_text(line.description),
            _quantity(line.quantity),
            _money(line.unit_price),
            _money(line.amount),
            _money(line.tax_amount),
            line.account_code,
            line.account_name,
            _money(bill.tax),
            _money(bill.total),
            bill.posting_period or bill.invoice_date.strftime("%Y-%m"),
        ]
        for bill in bills
        for line in bill.lines
    ]
    return _write(header, rows)


FORMATS: dict[str, ExportFormat] = {
    "xero": ExportFormat("xero", "Xero bills import (CSV)", "xero-bills", render_xero),
    "quickbooks": ExportFormat(
        "quickbooks", "QuickBooks Online bills import (CSV)", "quickbooks-bills", render_quickbooks
    ),
    "standard": ExportFormat(
        "standard", "Standard CSV (Excel, Google Sheets)", "invoices", render_standard
    ),
}
