"""Invoices with more than lines and a total: sections, shipping, several taxes, a deposit
already paid, a description, and notes, late-fee terms and remittance details."""

import csv
import io
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from tests.fakes import acme_invoice_fields, install_fakes, wire_answers

FULL_ACCOUNT = "4417926819"


def _sectioned(**overrides: Any) -> dict[str, Any]:
    """What the model returns for Acme's two-section invoice (see the module docstring)."""
    fields = acme_invoice_fields(
        invoice_number="AS-21300",
        description="Office paper and printer supplies, October 2026",
        description_printed=True,
        bill_to_name="Northwind Analytics Inc",
        subtotal="2323.50",
        shipping="35.00",
        tax="179.14",  # state 67.38 + city 111.76
        total="2537.64",
        amount_paid="500.00",
        amount_due="2037.64",
        line_items=[
            {"description": "Copier paper, 20 lb, case of 10 reams", "quantity": 50, "unit_price": "24.99", "amount": "1249.50"},
            {"description": "Toner cartridge, black, high yield", "quantity": 12, "unit_price": "89.50", "amount": "1074.00"},
        ],
        payment_terms_text="2% 10, Net 30",
        late_fee_terms="Late payments are subject to a 1.5% monthly finance charge.",
        remittance_details=f"Remit to: Acme Supplies Inc, Routing 021000021 Account {FULL_ACCOUNT}",
        notes=["Thank you for your business."],
        other_details=[
            {"label": "Order number", "value": "SO-55123"},
            {"label": "Sales rep", "value": "Dana Price"},
        ],
    )  # fmt: skip
    fields.update(overrides)
    return fields


def _process(app: FastAPI, client: TestClient, settings: Settings, **overrides: Any) -> Any:
    install_fakes(app, settings, _sectioned(**overrides), jev=[wire_answers()])
    return client.post(
        "/api/v1/invoices", files={"file": ("sections.txt", b"INVOICE AS-21300", "text/plain")}
    ).json()


def test_shipping_taxes_and_a_deposit_add_up(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body = _process(app, client, seeded_settings)

    assert "TOTALS_MISMATCH" not in body["decision"]["reason_codes"]
    assert body["status"] == "approved"  # clean, matched, and under the 5,000 limit
    assert (body["shipping"], body["amount_paid"], body["amount_due"]) == (
        "35.00",
        "500.00",
        "2037.64",
    )
    assert body["description"] == "Office paper and printer supplies, October 2026"


def test_invoice_page_shows_description_and_additional_information(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body = _process(app, client, seeded_settings)

    page = client.get(f"/invoices/{body['id']}").text
    journey = client.get(f"/invoices/{body['id']}/steps").text

    assert "Office paper and printer supplies, October 2026" in page
    assert "Payment terms" in page  # step 4 shows everything that was read
    assert "2% 10, Net 30" in page
    assert "1.5% monthly finance charge" in page
    assert "Thank you for your business." in page
    assert "Order number" in page and "SO-55123" in page
    assert "Sales rep" in page and "Dana Price" in page
    assert "Routing 021000021 Account ••••6819" in page
    assert "Amount to pay" in page and "USD 2,037.64" in page
    # The full account number is stored and shown nowhere, not even in the raw model answer.
    assert FULL_ACCOUNT not in page and FULL_ACCOUNT not in journey
    assert FULL_ACCOUNT not in str(body)


def test_invoice_addressed_to_another_company_goes_to_a_person(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body = _process(app, client, seeded_settings, bill_to_name="Northwind Holdings LLC")

    assert "WRONG_BILL_TO" in body["decision"]["reason_codes"]
    assert body["status"] == "pending_review"


def test_export_puts_shipping_on_its_own_line(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    _process(app, client, seeded_settings)
    batch = client.post("/api/v1/exports", json={"format": "standard", "created_by": "maya"}).json()

    text = client.get(f"/api/v1/exports/{batch['id']}/file").content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))[1:]

    shipping = next(row for row in rows if "Shipping and handling" in row)
    assert "35.00" in shipping and "6800" in shipping  # Courier and Postage
    assert any("Sales tax" in row for row in rows)
    assert sum(Decimal(row[8]) for row in rows) == Decimal("2537.64")  # the invoice total
