"""Vendor, purchase-order, and export screens."""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from tests.fakes import acme_invoice_fields, install_fakes, wire_answers


def _post(client: TestClient, url: str, data: dict[str, str]) -> str:
    response = client.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    location: str = response.headers["location"]
    return location


def _vendor(client: TestClient) -> str:
    location = _post(
        client,
        "/vendors",
        {
            "name": "Northgate Cleaning Ltd",
            "default_currency": "GBP",
            "payment_terms_days": "14",
            "default_gl_account_code": "6400",
            "bank_account": "GB12 ABCD 1020 3040 5060 70",
            "person": "Maya",
        },
    )
    return location.split("?")[0]


def test_vendors_page_lists_seed_vendors_and_forms(client: TestClient) -> None:
    page = client.get("/vendors").text

    assert "Acme Supplies Inc" in page
    assert "ending 6819" in page
    assert "Blocked" in page  # Vortex Trading
    assert 'action="/vendors/import"' not in page


def test_add_vendor_from_the_page(client: TestClient) -> None:
    path = _vendor(client)

    page = client.get(path + "?notice=vendor_created").text
    assert "Vendor added." in page
    assert "pays into account ending 6070" in page
    assert "Vendor added" in page  # history


def test_add_vendor_error_keeps_what_was_typed(client: TestClient) -> None:
    response = client.post(
        "/vendors",
        data={"name": "Acme Supplies Limited", "default_gl_account_code": "6000", "person": "Maya"},
    )

    assert response.status_code == 400
    assert "already exists" in response.text
    assert 'value="Acme Supplies Limited"' in response.text


def test_bank_change_from_the_page_needs_a_note(client: TestClient) -> None:
    path = _vendor(client)

    refused = client.post(
        f"{path}/bank-account",
        data={"bank_account": "GB99 ZZZZ 1111 2222 3333 44", "person": "Sam"},
    )
    changed = client.post(
        f"{path}/bank-account",
        data={
            "bank_account": "GB99 ZZZZ 1111 2222 3333 44",
            "note": "Called Northgate on our known number; confirmed.",
            "person": "Sam",
        },
    )

    assert refused.status_code == 400
    assert "how the new account was verified" in refused.text
    assert "ending 3344" in changed.text
    assert "Account ending 6070 changed to ending 3344" in changed.text


def test_create_po_receive_and_close_from_the_pages(client: TestClient) -> None:
    vendor_id = _vendor(client).rsplit("/", 1)[1]
    po_path = _post(
        client,
        "/purchase-orders",
        {
            "po_number": "PO-4001",
            "vendor_id": vendor_id,
            "issued_date": "2026-09-20",
            "lines": "Office cleaning, October | 1 | 450.00\nWindow cleaning | 2 | 75 | 6400",
            "needs_receiving": "1",  # the box is ticked by default on the form
            "person": "Maya",
        },
    ).split("?")[0]
    page = client.get(po_path).text
    assert "PO-4001" in page and "Open, nothing received" in page
    line_ids = re.findall(r'name="qty_([0-9a-f-]+)"', page)
    assert len(line_ids) == 2

    received = client.post(
        f"{po_path}/receipts",
        data={f"qty_{line_ids[0]}": "1", "received_date": "2026-10-01", "person": "Jo"},
    )
    assert "Partly received" in received.text
    assert "Delivery recorded" in received.text  # history

    closed = client.post(f"{po_path}/status", data={"status": "closed", "person": "Sam"})
    assert "Closed" in closed.text
    assert 'name="qty_' not in closed.text


def test_po_form_reports_bad_lines(client: TestClient) -> None:
    vendor_id = _vendor(client).rsplit("/", 1)[1]

    response = client.post(
        "/purchase-orders",
        data={
            "po_number": "PO-4002",
            "vendor_id": vendor_id,
            "lines": "just a description",
            "person": "M",
        },
    )

    assert response.status_code == 400
    assert "description | quantity | unit price" in response.text


def test_create_po_with_separate_item_fields(client: TestClient) -> None:
    vendor_id = _vendor(client).rsplit("/", 1)[1]
    response = client.post(
        "/purchase-orders",
        data={
            "po_number": "PO-4010",
            "vendor_id": vendor_id,
            "person": "Sam",
            "item_description": ["Cleaning | windows", "Facilities inspection"],
            "item_quantity": ["2", "1"],
            "item_price": ["75.50", "100"],
            "item_account": ["6400", ""],
        },
    )
    assert response.status_code == 200
    assert "Cleaning | windows" in response.text
    assert "Facilities inspection" in response.text
    assert "251.00" in response.text
    assert "Open, no receiving" in response.text


def test_po_item_error_preserves_entered_values(client: TestClient) -> None:
    vendor_id = _vendor(client).rsplit("/", 1)[1]
    response = client.post(
        "/purchase-orders",
        data={
            "po_number": "PO-4011",
            "vendor_id": vendor_id,
            "person": "Sam",
            "item_description": "Window cleaning",
            "item_quantity": "2",
            "item_price": "not-a-price",
            "item_account": "6400",
            "needs_receiving": "1",
        },
    )
    assert response.status_code == 400
    assert "enter a valid quantity and unit price" in response.text
    assert 'value="Window cleaning"' in response.text
    assert 'value="not-a-price"' in response.text
    assert 'value="6400" selected' in response.text


def test_po_rejects_incomplete_item_fields(client: TestClient) -> None:
    vendor_id = _vendor(client).rsplit("/", 1)[1]
    response = client.post(
        "/purchase-orders",
        data={
            "po_number": "PO-4012",
            "vendor_id": vendor_id,
            "person": "Sam",
            "item_description": ["Cleaning", "Repair"],
            "item_quantity": "1",
            "item_price": "100",
            "item_account": "",
        },
    )
    assert response.status_code == 400
    assert "Each item needs" in response.text


def test_po_list_shows_seed_orders(client: TestClient) -> None:
    page = client.get("/purchase-orders").text

    assert "PO-1003" in page and "Fully received" in page
    assert "PO-1002" in page and "Partly received" in page
    assert "PO-1005" in page and "Open, no receiving" in page  # a campaign: a service


def test_export_page_creates_file_and_marks_invoices(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])
    invoice = client.post(
        "/api/v1/invoices", files={"file": ("a.txt", b"INVOICE AS-20431", "text/plain")}
    ).json()
    assert invoice["status"] == "approved"

    page = client.get("/exports").text
    assert "AS-20431" in page and "Ready to export" in page

    response = client.post(
        "/exports",
        data={"invoice_id": invoice["id"], "format": "xero", "person": "Maya"},
        follow_redirects=False,
    )
    after = client.get(response.headers["location"]).text
    download = re.search(r'href="(/exports/[0-9a-f-]+/download)"', after)

    assert response.status_code == 303
    assert "Export file created with 1 invoice" in after
    assert "Nothing to export" in after
    assert download is not None
    file = client.get(download.group(1))
    assert file.content.decode("utf-8-sig").startswith("*ContactName")
    assert client.get(f"/api/v1/invoices/{invoice['id']}").json()["status"] == "exported"


def test_export_without_name_is_refused(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])
    invoice = client.post(
        "/api/v1/invoices", files={"file": ("a.txt", b"INVOICE AS-20431", "text/plain")}
    ).json()

    response = client.post(
        "/exports", data={"invoice_id": invoice["id"], "format": "xero", "person": ""}
    )

    assert response.status_code == 400
    assert "Enter your name" in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [("item_quantity", "Infinity"), ("item_price", "Infinity"), ("item_price", "NaN")],
)
def test_po_rejects_nonfinite_items(client: TestClient, field: str, value: str) -> None:
    vendor_id = _vendor(client).rsplit("/", 1)[1]
    data = {
        "po_number": "PO-FINITE",
        "vendor_id": vendor_id,
        "person": "Sam",
        "item_description": "Cleaning",
        "item_quantity": "1",
        "item_price": "100",
        "item_account": "",
    }
    data[field] = value
    assert client.post("/purchase-orders", data=data).status_code == 400


def test_po_does_not_fall_back_when_item_description_missing(client: TestClient) -> None:
    vendor_id = _vendor(client).rsplit("/", 1)[1]
    response = client.post(
        "/purchase-orders",
        data={
            "po_number": "PO-MISSING",
            "vendor_id": vendor_id,
            "person": "Sam",
            "lines": "Cleaning | 1 | 100",
            "item_quantity": "1",
            "item_price": "100",
            "item_account": "",
        },
    )
    assert response.status_code == 400
    assert "Each item needs" in response.text
