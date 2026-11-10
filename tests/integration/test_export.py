"""Exporting approved invoices: bill building, file formats, and the once-only rule."""

import csv
import io
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from jev_invoice.db.models import Invoice, InvoiceLine, PurchaseOrder, PurchaseOrderLine, Vendor
from jev_invoice.domain.enums import InvoiceSource, InvoiceStatus
from jev_invoice.export.formats import (
    ExportBill,
    ExportLine,
    render_quickbooks,
    render_standard,
    render_xero,
    safe_text,
)
from jev_invoice.export.service import ExportService, _spread
from tests.conftest import make_settings
from tests.fakes import acme_invoice_fields, install_fakes, wire_answers

NAMES = {
    "6000": "Office Supplies",
    "6400": "Rent and Facilities",
    "9999": "Suspense - needs coding",
}


def _invoice(**overrides: Any) -> Invoice:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "status": InvoiceStatus.APPROVED,
        "source": InvoiceSource.UPLOAD,
        "original_filename": "x.pdf",
        "content_type": "application/pdf",
        "file_size_bytes": 1,
        "file_sha256": "0" * 64,
        "storage_key": "k",
        "vendor_name": "Nexus Software",
        "invoice_number": "NSS-1",
        "invoice_date": date(2026, 9, 30),
        "currency": "USD",
        "subtotal": Decimal("3290.00"),
        "discount": Decimal("164.50"),
        "tax_total": Decimal("625.10"),
        "total": Decimal("3750.60"),
        "lines": [
            InvoiceLine(line_no=1, description="CRM licence", quantity=Decimal(5), unit_price=Decimal("200"), amount=Decimal("1000.00")),
            InvoiceLine(line_no=2, description="Modules", quantity=Decimal(7), unit_price=Decimal("327.142857"), amount=Decimal("2290.00")),
        ],
    }  # fmt: skip
    values.update(overrides)
    invoice = Invoice(**{k: v for k, v in values.items() if k not in ("vendor", "purchase_order")})
    invoice.vendor = values.get("vendor")
    invoice.purchase_order = values.get("purchase_order")
    return invoice


@pytest.fixture
def service(tmp_path: Path) -> ExportService:
    """US defaults: sales tax is part of the cost."""
    return ExportService(None, make_settings(tmp_path))  # type: ignore[arg-type]


@pytest.fixture
def vat_service(tmp_path: Path) -> ExportService:
    """Where tax is reclaimable (VAT/GST): tax is spread over the lines."""
    return ExportService(None, make_settings(tmp_path, export_sales_tax_as_cost=False))  # type: ignore[arg-type]


# ── Building one bill ─────────────────────────────────────────────────────────


def test_us_sales_tax_becomes_a_cost_line(service: ExportService) -> None:
    bill = service.build_bill(_invoice(), NAMES)

    assert isinstance(bill, ExportBill)
    assert [line.description for line in bill.lines] == [
        "CRM licence",
        "Modules",
        "Discount",
        "Sales tax",
    ]
    assert bill.lines[-1].amount == Decimal("625.10")
    assert bill.tax == 0 and all(line.tax_amount == 0 for line in bill.lines)
    assert sum(line.amount for line in bill.lines) == bill.total
    assert {line.account_code for line in bill.lines} == {"9999"}  # coded like the purchase


def test_bill_keeps_lines_adds_discount_and_spreads_tax(vat_service: ExportService) -> None:
    bill = vat_service.build_bill(_invoice(), NAMES)

    assert isinstance(bill, ExportBill)
    assert [line.description for line in bill.lines] == ["CRM licence", "Modules", "Discount"]
    assert [line.amount for line in bill.lines] == [
        Decimal("1000.00"),
        Decimal("2290.00"),
        Decimal("-164.50"),
    ]
    assert sum(line.amount for line in bill.lines) + bill.tax == bill.total
    assert sum(line.tax_amount for line in bill.lines) == Decimal("625.10")
    # 7 x 327.142857 does not equal 2290.00 exactly, so it exports as 1 x 2290.00
    assert (bill.lines[1].quantity, bill.lines[1].unit_price) == (Decimal(1), Decimal("2290.00"))
    assert bill.lines[0].account_code == "9999"  # no vendor or PO: suspense


def test_lines_that_do_not_reconcile_become_one_summary_line(vat_service: ExportService) -> None:
    bill = vat_service.build_bill(_invoice(total=Decimal("4000.00")), NAMES)

    assert isinstance(bill, ExportBill)
    assert len(bill.lines) == 1
    assert bill.lines[0].description == "Invoice NSS-1"
    assert bill.lines[0].amount == Decimal("3374.90")  # 4000.00 - 625.10 tax


def test_account_from_po_then_vendor(service: ExportService) -> None:
    vendor = Vendor(
        name="Acme", default_gl_account_code="6400", payment_terms_days=14, default_currency="USD"
    )
    po = PurchaseOrder(
        lines=[PurchaseOrderLine(gl_account_code="6000"), PurchaseOrderLine(gl_account_code="6000")]
    )
    mixed_po = PurchaseOrder(
        lines=[PurchaseOrderLine(gl_account_code="6000"), PurchaseOrderLine(gl_account_code="1500")]
    )

    from_po = service.build_bill(_invoice(vendor=vendor, purchase_order=po), NAMES)
    from_vendor = service.build_bill(_invoice(vendor=vendor, purchase_order=mixed_po), NAMES)

    assert isinstance(from_po, ExportBill) and isinstance(from_vendor, ExportBill)
    assert from_po.lines[0].account_code == "6000"
    assert from_po.lines[0].account_name == "Office Supplies"
    assert from_vendor.lines[0].account_code == "6400"
    assert from_vendor.supplier == "Acme"


def test_due_date_falls_back_to_vendor_terms(service: ExportService) -> None:
    vendor = Vendor(
        name="Acme", default_gl_account_code="6400", payment_terms_days=14, default_currency="USD"
    )

    bill = service.build_bill(_invoice(vendor=vendor, due_date=None), NAMES)

    assert isinstance(bill, ExportBill)
    assert bill.due_date == date(2026, 10, 14)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"invoice_number": None}, "no invoice number"),
        ({"invoice_date": None}, "no invoice date"),
        ({"total": None}, "no total"),
        ({"currency": None}, "no currency"),
        ({"vendor_name": None}, "no supplier name"),
    ],
)
def test_reasons_an_invoice_cannot_be_exported(
    service: ExportService, overrides: dict[str, Any], reason: str
) -> None:
    assert service.build_bill(_invoice(**overrides), NAMES) == reason


def test_tax_spread_rounding_lands_on_last_line() -> None:
    assert _spread(Decimal("10.00"), [Decimal(1), Decimal(1), Decimal(1)]) == [
        Decimal("3.33"), Decimal("3.33"), Decimal("3.34"),
    ]  # fmt: skip
    assert _spread(Decimal("0"), [Decimal(5)]) == [Decimal("0")]


# ── File formats ──────────────────────────────────────────────────────────────


def _bill(supplier: str = "Acme Supplies Inc", tax: str = "0.00") -> ExportBill:
    return ExportBill(
        supplier=supplier,
        invoice_number="AS-1",
        invoice_date=date(2026, 3, 5),
        due_date=date(2026, 4, 4),
        currency="USD",
        tax=Decimal(tax),
        total=Decimal("100.00") + Decimal(tax),
        lines=(
            ExportLine("Paper", Decimal(4), Decimal("25.00"), Decimal("100.00"), Decimal(tax), "6000", "Office Supplies"),
        ),
    )  # fmt: skip


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_xero_format(tmp_path: Path) -> None:
    rows = _rows(render_xero([_bill(), _bill(tax="20.00")], make_settings(tmp_path)))

    assert rows[0] == ["*ContactName", "*InvoiceNumber", "*InvoiceDate", "*DueDate", "Description", "*Quantity", "*UnitAmount", "*AccountCode", "*TaxType", "Currency"]  # fmt: skip
    assert rows[1] == ["Acme Supplies Inc", "AS-1", "05-Mar-2026", "04-Apr-2026", "Paper", "4", "25.00", "6000", "Tax Exempt", "USD"]  # fmt: skip
    assert rows[2][8] == "Tax on Purchases"


def test_quickbooks_format(tmp_path: Path) -> None:
    rows = _rows(render_quickbooks([_bill(tax="20.00")], make_settings(tmp_path)))

    assert rows[0][:7] == ["Bill no.", "Supplier", "Bill Date", "Due Date", "Account", "Line Description", "Line Amount"]  # fmt: skip
    assert rows[1] == ["AS-1", "Acme Supplies Inc", "03/05/2026", "04/04/2026", "Office Supplies", "Paper", "100.00", "TAX", "20.00", "USD"]  # fmt: skip


def test_standard_format_has_every_field(tmp_path: Path) -> None:
    rows = _rows(render_standard([_bill()], make_settings(tmp_path)))

    assert rows[0][0] == "supplier"
    assert rows[1][2] == "2026-03-05"
    assert rows[1][rows[0].index("line_amount")] == "100.00"
    assert rows[1][rows[0].index("posting_period")] == "2026-03"


@pytest.mark.parametrize("hostile", ['=HYPERLINK("http://x")', "+1+1", "-2+3", "@SUM(A1)"])
def test_spreadsheet_formulas_from_invoices_are_neutralised(tmp_path: Path, hostile: str) -> None:
    rows = _rows(render_standard([_bill(supplier=hostile)], make_settings(tmp_path)))

    assert rows[1][0] == "'" + hostile
    assert safe_text("Acme") == "Acme"


# ── Through the API ───────────────────────────────────────────────────────────


def _approved_invoice(app: FastAPI, client: TestClient, settings: Settings, number: str) -> str:
    install_fakes(app, settings, acme_invoice_fields(invoice_number=number), jev=[wire_answers()])
    body = client.post(
        "/api/v1/invoices", files={"file": (f"{number}.txt", number.encode(), "text/plain")}
    ).json()
    assert body["status"] == "approved", body
    invoice_id: str = body["id"]
    return invoice_id


def test_export_marks_invoices_and_cannot_repeat(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    first = _approved_invoice(app, client, seeded_settings, "AS-90001")
    ready = client.get("/api/v1/exports/ready").json()
    assert [item["invoice_id"] for item in ready if item["ready"]] == [first]

    created = client.post(
        "/api/v1/exports", json={"format": "xero", "invoice_ids": [first], "created_by": "maya"}
    )
    again = client.post(
        "/api/v1/exports", json={"format": "xero", "invoice_ids": [first], "created_by": "maya"}
    )

    assert created.status_code == 201
    batch = created.json()
    assert batch["invoice_count"] == 1
    assert batch["filename"].startswith("xero-bills-")
    assert client.get(f"/api/v1/invoices/{first}").json()["status"] == "exported"
    assert again.status_code == 422
    assert "not waiting for export" in again.json()["error"]["message"]
    assert client.get("/api/v1/exports/ready").json() == []


def test_export_file_download(app: FastAPI, client: TestClient, seeded_settings: Settings) -> None:
    _approved_invoice(app, client, seeded_settings, "AS-90002")
    batch = client.post(
        "/api/v1/exports", json={"format": "quickbooks", "created_by": "maya"}
    ).json()

    download = client.get(f"/api/v1/exports/{batch['id']}/file")

    assert download.headers["content-type"].startswith("text/csv")
    assert "attachment" in download.headers["content-disposition"]
    text = download.content.decode("utf-8-sig")
    rows = _rows(text)
    assert rows[0][0] == "Bill no."
    assert rows[1][:2] == ["AS-90002", "Acme Supplies Inc"]
    assert rows[1][4] == "Office Supplies"  # PO-1003's account (6000), by name for QuickBooks
    assert [b["id"] for b in client.get("/api/v1/exports").json()] == [batch["id"]]


def test_export_rejects_unknown_format_and_empty_selection(client: TestClient) -> None:
    unknown = client.post(
        "/api/v1/exports",
        json={"format": "sage", "invoice_ids": [str(uuid.uuid4())], "created_by": "m"},
    )
    empty = client.post("/api/v1/exports", json={"format": "xero", "created_by": "m"})

    assert unknown.status_code == 422
    assert empty.status_code == 422
    assert "at least one invoice" in empty.json()["error"]["message"]
