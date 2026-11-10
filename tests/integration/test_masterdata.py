"""Vendors, purchase orders, deliveries and CSV import, through the API."""

import asyncio
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from jev_invoice.config import Settings
from jev_invoice.db.models import AuditEvent
from jev_invoice.db.session import create_engine, create_session_factory
from tests.fakes import install_fakes, wire_answers

NEW_BANK = "GB12 ABCD 1020 3040 5060 70"


def _audit(settings: Settings, action: str) -> list[dict[str, Any]]:
    async def load() -> list[dict[str, Any]]:
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session:
                rows = await session.scalars(select(AuditEvent).where(AuditEvent.action == action))
                return [{"actor": row.actor, **row.data} for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(load())


def _vendor(client: TestClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "name": "Northgate Cleaning Ltd",
        "default_currency": "usd",
        "payment_terms_days": 14,
        "default_gl_account_code": "6400",
        "bank_account": NEW_BANK,
        "created_by": "maya",
    }
    body.update(overrides)
    response = client.post("/api/v1/vendors", json=body)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def _po(client: TestClient, vendor_id: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "po_number": "PO-3001",
        "vendor_id": vendor_id,
        "issued_date": "2026-09-20",
        "lines": [
            {"description": "Office cleaning, October", "quantity": 1, "unit_price": "450.00"},
            {"description": "Window cleaning", "quantity": 2, "unit_price": "75.00"},
        ],
        "created_by": "maya",
    }
    body.update(overrides)
    response = client.post("/api/v1/purchase-orders", json=body)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


# ── Vendors ───────────────────────────────────────────────────────────────────


def test_create_vendor_stores_only_last4_of_bank_account(
    client: TestClient, seeded_settings: Settings
) -> None:
    vendor = _vendor(client)

    assert vendor["default_currency"] == "USD"
    assert vendor["bank_account_last4"] == "6070"
    assert "1020 3040" not in str(vendor)
    [event] = _audit(seeded_settings, "vendor.created")
    assert event["actor"] == "maya"


def test_vendor_name_must_be_unique_after_normalising(client: TestClient) -> None:
    response = client.post(
        "/api/v1/vendors",
        json={
            "name": "ACME SUPPLIES LIMITED",
            "default_gl_account_code": "6000",
            "created_by": "maya",
        },
    )

    assert response.status_code == 409
    assert "Acme Supplies Inc" in response.json()["error"]["message"]


def test_vendor_validation(client: TestClient) -> None:
    unknown_gl = client.post(
        "/api/v1/vendors",
        json={"name": "X Ltd", "default_gl_account_code": "0000", "created_by": "m"},
    )
    bad_currency = client.post(
        "/api/v1/vendors",
        json={
            "name": "Y Ltd",
            "default_gl_account_code": "6000",
            "default_currency": "POUNDS",
            "created_by": "m",
        },
    )

    assert unknown_gl.status_code == 422
    assert bad_currency.status_code == 422


def test_update_vendor_records_what_changed(client: TestClient, seeded_settings: Settings) -> None:
    vendor = _vendor(client)

    response = client.put(
        f"/api/v1/vendors/{vendor['id']}",
        json={
            "name": vendor["name"],
            "default_currency": "USD",
            "payment_terms_days": 30,
            "default_gl_account_code": "6400",
            "status": "on_hold",
            "changed_by": "sam",
        },
    )

    assert response.json()["status"] == "on_hold"
    [event] = _audit(seeded_settings, "vendor.updated")
    assert event["changes"]["status"] == ["active", "on_hold"]
    assert event["changes"]["payment_terms_days"] == ["14", "30"]


def test_bank_change_needs_a_verification_note(
    client: TestClient, seeded_settings: Settings
) -> None:
    vendor = _vendor(client)
    url = f"/api/v1/vendors/{vendor['id']}/bank-account"

    no_note = client.put(
        url, json={"bank_account": "GB99 ZZZZ 1111 2222 3333 44", "changed_by": "sam"}
    )
    changed = client.put(
        url,
        json={
            "bank_account": "GB99 ZZZZ 1111 2222 3333 44",
            "changed_by": "sam",
            "note": "Called Northgate on 0113 000 0000 from our records; confirmed new account.",
        },
    )

    assert no_note.status_code == 422
    assert changed.json()["bank_account_last4"] == "3344"
    [event] = _audit(seeded_settings, "vendor.bank_account_changed")
    assert (event["old_last4"], event["new_last4"], event["actor"]) == ("6070", "3344", "sam")


# ── Purchase orders and deliveries ────────────────────────────────────────────


def test_create_po_totals_lines_and_uses_vendor_gl(client: TestClient) -> None:
    po = _po(client, _vendor(client)["id"])

    assert po["status"] == "open"
    assert po["total_amount"] == "600.00"
    assert po["currency"] == "USD"
    assert {line["gl_account_code"] for line in po["lines"]} == {"6400"}
    assert po["received_value"] == "0.00"


def test_po_number_must_be_unique(client: TestClient) -> None:
    vendor_id = _vendor(client)["id"]
    _po(client, vendor_id)

    response = client.post(
        "/api/v1/purchase-orders",
        json={
            "po_number": "po 3001",
            "vendor_id": vendor_id,
            "issued_date": "2026-09-20",
            "lines": [{"description": "x", "quantity": 1, "unit_price": 1}],
            "created_by": "m",
        },
    )

    assert response.status_code == 409


def test_deliveries_move_po_to_partially_then_fully_received(client: TestClient) -> None:
    po = _po(client, _vendor(client)["id"])
    cleaning, windows = po["lines"]
    url = f"/api/v1/purchase-orders/{po['id']}/receipts"

    partial = client.post(
        url,
        json={
            "received_date": "2026-10-01",
            "received_by": "jo",
            "quantities": {cleaning["id"]: 1},
        },
    ).json()
    full = client.post(
        url,
        json={"received_date": "2026-10-02", "received_by": "jo", "quantities": {windows["id"]: 2}},
    ).json()

    assert partial["status"] == "partially_received"
    assert partial["received_value"] == "450.00"
    assert full["status"] == "received"
    assert full["received_value"] == "600.00"


def test_cannot_receive_more_than_ordered(client: TestClient) -> None:
    po = _po(client, _vendor(client)["id"])

    response = client.post(
        f"/api/v1/purchase-orders/{po['id']}/receipts",
        json={
            "received_date": "2026-10-01",
            "received_by": "jo",
            "quantities": {po["lines"][1]["id"]: 3},
        },
    )

    assert response.status_code == 422
    assert "only 2 is still outstanding" in response.json()["error"]["message"]


def test_cancel_and_close_rules(client: TestClient) -> None:
    vendor_id = _vendor(client)["id"]
    received = _po(client, vendor_id)
    client.post(
        f"/api/v1/purchase-orders/{received['id']}/receipts",
        json={
            "received_date": "2026-10-01",
            "received_by": "jo",
            "quantities": {received["lines"][0]["id"]: 1},
        },
    )
    untouched = _po(client, vendor_id, po_number="PO-3002")

    cancel_received = client.put(
        f"/api/v1/purchase-orders/{received['id']}/status",
        json={"status": "cancelled", "changed_by": "sam"},
    )
    close_received = client.put(
        f"/api/v1/purchase-orders/{received['id']}/status",
        json={"status": "closed", "changed_by": "sam", "note": "Windows no longer needed"},
    )
    cancel_untouched = client.put(
        f"/api/v1/purchase-orders/{untouched['id']}/status",
        json={"status": "cancelled", "changed_by": "sam"},
    )
    deliver_cancelled = client.post(
        f"/api/v1/purchase-orders/{untouched['id']}/receipts",
        json={
            "received_date": "2026-10-01",
            "received_by": "jo",
            "quantities": {untouched["lines"][0]["id"]: 1},
        },
    )

    assert cancel_received.status_code == 409
    assert close_received.json()["status"] == "closed"
    assert cancel_untouched.json()["status"] == "cancelled"
    assert deliver_cancelled.status_code == 409


# ── CSV import ────────────────────────────────────────────────────────────────


def _import(client: TestClient, kind: str, csv_text: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/{kind}/import",
        params={"imported_by": "maya"},
        files={"file": ("data.csv", csv_text.encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def test_templates_download(client: TestClient) -> None:
    vendors = client.get("/api/v1/vendors/import-template.csv")
    pos = client.get("/api/v1/purchase-orders/import-template.csv")

    assert vendors.text.startswith("name,currency,payment_terms_days,default_gl_account")
    assert pos.text.startswith("po_number,vendor,issued_date")
    assert "attachment" in vendors.headers["content-disposition"]


def test_vendor_import_creates_all_rows(client: TestClient) -> None:
    csv_text = (
        "﻿Name,Currency,payment_terms_days,default_gl_account,bank_account\n"
        "Northgate Cleaning Ltd,USD,14,6400,GB12 ABCD 1020 3040 5060 70\n"
        "Kestrel Print Co,USD,,6200,\n"
    )

    report = _import(client, "vendors", csv_text)

    assert report == {"created": 2, "errors": []}
    names = {vendor["name"] for vendor in client.get("/api/v1/vendors").json()}
    assert {"Northgate Cleaning Ltd", "Kestrel Print Co"} <= names


def test_vendor_import_is_all_or_nothing_with_row_numbers(client: TestClient) -> None:
    csv_text = (
        "name,currency,default_gl_account\n"
        "Fine Vendor Ltd,USD,6000\n"
        "Acme Supplies Limited,USD,6000\n"
        "Bad Currency Ltd,POUNDS,6000\n"
        "Fine Vendor Limited,USD,6000\n"
    )

    report = _import(client, "vendors", csv_text)

    assert report["created"] == 0
    assert any(
        error.startswith("Row 3: Acme Supplies Limited already exists")
        for error in report["errors"]
    )
    assert any(error.startswith("Row 4:") and "currency" in error for error in report["errors"])
    assert any("also appears on row 2" in error for error in report["errors"])
    assert "Fine Vendor Ltd" not in {v["name"] for v in client.get("/api/v1/vendors").json()}


def test_import_reports_missing_columns(client: TestClient) -> None:
    report = _import(client, "vendors", "name,currency\nX,USD\n")

    assert report["errors"] == ["Missing columns: default_gl_account."]


def test_po_import_groups_lines_by_po_number(client: TestClient) -> None:
    csv_text = (
        "po_number,vendor,issued_date,currency,description,quantity,unit_price,gl_account\n"
        "PO-2001,Acme Supplies Inc,2026-10-01,USD,Paper,20,24.99,6000\n"
        "PO-2001,Acme Supplies Inc,2026-10-01,USD,Toner,4,89.50,6000\n"
        "PO-2002,Halcyon Computing LLC,2026-10-01,,Laptop bag,5,40.00,\n"
    )

    report = _import(client, "purchase-orders", csv_text)

    assert report == {"created": 2, "errors": []}
    pos = {po["po_number"]: po for po in client.get("/api/v1/purchase-orders").json()}
    assert len(pos["PO-2001"]["lines"]) == 2
    assert pos["PO-2001"]["total_amount"] == "857.80"
    assert pos["PO-2002"]["lines"][0]["gl_account_code"] == "1500"  # Halcyon's default


def test_po_import_unknown_vendor(client: TestClient) -> None:
    csv_text = (
        "po_number,vendor,issued_date,description,quantity,unit_price\n"
        "PO-2003,Nobody Ltd,2026-10-01,Thing,1,10\n"
    )

    report = _import(client, "purchase-orders", csv_text)

    assert report["created"] == 0
    assert "Nobody Ltd" in report["errors"][0]


# ── New records drive invoice decisions ───────────────────────────────────────


def test_new_vendor_po_and_delivery_let_invoice_auto_approve(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    vendor = _vendor(client)
    po = _po(client, vendor["id"])
    client.post(
        f"/api/v1/purchase-orders/{po['id']}/receipts",
        json={
            "received_date": "2026-10-01",
            "received_by": "jo",
            "quantities": {line["id"]: line["quantity"] for line in po["lines"]},
        },
    )
    install_fakes(
        app,
        seeded_settings,
        {
            "vendor_name": "Northgate Cleaning Ltd",
            "invoice_number": "NC-501",
            "invoice_date": "2026-10-02",
            "currency": "USD",
            "po_reference": "PO-3001",
            "bank_account": NEW_BANK,
            "subtotal": 600,
            "tax": 0,
            "total": 600,
            # Lines that match the PO's, so each one is matched and coded from the PO.
            "line_items": [
                {
                    "description": "Office cleaning, October",
                    "quantity": 1,
                    "unit_price": 450,
                    "amount": 450,
                },
                {"description": "Window cleaning", "quantity": 2, "unit_price": 75, "amount": 150},
            ],
        },
        jev=[wire_answers()],
    )

    body = client.post(
        "/api/v1/invoices", files={"file": ("nc.txt", b"INVOICE NC-501 Northgate", "text/plain")}
    ).json()

    assert body["decision"]["reason_codes"] == []
    assert body["status"] == "approved"
    assert body["vendor_matched"] is True
    assert body["purchase_order_number"] == "PO-3001"
