"""Receiving: record deliveries, and invoices waiting for goods are checked again."""

import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.config import Settings
from jev_invoice.db.seed import seed_id
from jev_invoice.domain.enums import ReasonCode
from tests.fakes import acme_invoice_fields, install_fakes, wire_answers
from tests.integration.test_matching import _codes, _match

PO_1008 = seed_id("po", "PO-1008")  # 12 hours of contract review ordered, 8 received
PO_1008_LINE = seed_id("po_line", "PO-1008:1")
MERIDIAN_BANK = "Routing 026009593 Account 1555555555"  # matches seed data


def _meridian_twelve_hours() -> dict[str, Any]:
    return acme_invoice_fields(
        vendor_name="Meridian Legal LLP",
        invoice_number="ML-412",
        po_reference="PO-1008",
        bank_account=MERIDIAN_BANK,
        subtotal="3300.00",
        total="3300.00",
        line_items=[
            {
                "description": "Commercial contract review, hourly",
                "quantity": 12,
                "unit_price": "275.00",
                "amount": "3300.00",
            }
        ],
    )


def _deliver(client: TestClient, hours: str) -> Any:
    return client.post(
        f"/receiving/{PO_1008}",
        data={f"qty_{PO_1008_LINE}": hours, "received_date": "2026-10-02", "person": "Sam"},
        follow_redirects=False,
    )


def test_receiving_lists_what_is_still_to_come(client: TestClient) -> None:
    page = client.get("/receiving").text
    waiting, recent = page.split('<section aria-labelledby="done-title">', 1)

    assert "PO-1008" in waiting and "Meridian Legal LLP" in waiting
    assert "PO-1002" in waiting  # one of two servers delivered
    assert "PO-1005" not in waiting  # a marketing campaign: a service, nothing to receive
    assert "PO-1001" not in waiting  # fully received: nothing to come
    assert "GRN-5001" in recent
    assert f'name="qty_{PO_1008_LINE}"' in page
    assert "GRN-5008" in page  # recent deliveries


def test_delivery_rechecks_the_waiting_invoice_without_calling_models(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    llm, jev = install_fakes(app, seeded_settings, _meridian_twelve_hours(), jev=[wire_answers()])
    body = client.post(
        "/api/v1/invoices", files={"file": ("ML-412.txt", b"ML-412", "text/plain")}
    ).json()
    assert body["decision"]["reason_codes"] == ["QUANTITY_VARIANCE"]  # 12 billed, 8 received
    assert "ML-412" in client.get("/receiving").text  # shown as waiting on the order

    calls_before_delivery = (len(llm.calls), len(jev.requests))
    response = _deliver(client, "4")

    assert response.status_code == 303
    assert "approved=ML-412" in response.headers["location"]
    after = client.get(f"/api/v1/invoices/{body['id']}").json()
    assert after["status"] == "approved"
    assert after["decision"]["reason_codes"] == []
    assert calls_before_delivery[0] == 1
    assert (len(llm.calls), len(jev.requests)) == calls_before_delivery
    page = client.get(f"/invoices/{body['id']}").text
    assert "Checked again" in page and "every check passes now" in page
    assert client.get("/api/v1/review").json() == []


def test_a_partial_delivery_keeps_the_invoice_waiting_with_new_reasons(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, _meridian_twelve_hours(), jev=[wire_answers()])
    body = client.post(
        "/api/v1/invoices", files={"file": ("ML-412.txt", b"ML-412", "text/plain")}
    ).json()

    response = _deliver(client, "2")  # 10 of 12 hours now received

    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["checked"] == ["ML-412"] and "approved" not in query
    after = client.get(f"/api/v1/invoices/{body['id']}").json()
    assert after["status"] == "pending_review"
    assert after["decision"]["reason_codes"] == ["QUANTITY_VARIANCE"]
    assert "10 received" in str(after["decision"]["findings"])


def test_delivery_recheck_cannot_auto_approve_untrusted_gl_coding(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    llm, jev = install_fakes(app, seeded_settings, _meridian_twelve_hours(), jev=[wire_answers()])
    body = client.post(
        "/api/v1/invoices", files={"file": ("ML-412.txt", b"ML-412", "text/plain")}
    ).json()
    database = seeded_settings.database_url.removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE invoice_lines SET gl_source = 'vendor'")
    calls = (len(llm.calls), len(jev.requests))
    assert _deliver(client, "4").status_code == 303
    after = client.get(f"/api/v1/invoices/{body['id']}").json()
    assert after["status"] == "pending_review"
    assert after["decision"]["reason_codes"] == ["GL_CODING_REVIEW"]
    assert after["decision"]["exception_routing"][0]["owner"] == "Finance"
    assert (len(llm.calls), len(jev.requests)) == calls


def test_more_than_is_still_to_come_is_refused(client: TestClient) -> None:
    response = _deliver(client, "5")  # only 4 hours are still to come

    assert response.status_code == 400
    assert "only 4 is still outstanding" in response.text


# ── Orders that need no receiving (services) ──────────────────────────────────

BRIGHTLINE = {
    "vendor_name": "Brightline Cloud Services Inc",
    "invoice_number": "BCS-1",
    "po_reference": "PO-1004",  # hosting: a service, nothing is received
    "bank_account": "Routing 021000021 Account 4417 2209",
    "subtotal": "3200.00",
    "total": "3200.00",
}
HOSTING = {"description": "Managed cloud hosting, September 2026", "unit_price": "3200.00"}


async def test_a_service_order_is_matched_to_the_order_only(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(
        session,
        settings,
        **BRIGHTLINE,
        line_items=[{**HOSTING, "quantity": 1, "amount": "3200.00"}],
    )

    assert _codes(result) == []  # nothing was received, and nothing needs to be
    checks = {check.name: check for check in result.checks}
    assert checks["line_received"].label == "Billed within what was ordered"
    assert "Two-way match" in checks["line_received"].detail


async def test_a_service_order_still_catches_billing_more_than_ordered(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(
        session,
        settings,
        **{**BRIGHTLINE, "subtotal": "6400.00", "total": "6400.00"},
        line_items=[{**HOSTING, "quantity": 2, "amount": "6400.00"}],
    )

    assert _codes(result) == [ReasonCode.QUANTITY_VARIANCE]
    assert "only 1 ordered" in result.findings[0].message


def test_receiving_can_be_switched_off_and_deliveries_are_then_refused(
    client: TestClient,
) -> None:
    response = client.post(
        f"/purchase-orders/{PO_1008}/receiving", data={"person": "Sam"}, follow_redirects=False
    )
    assert response.status_code == 303

    page = client.get(f"/purchase-orders/{PO_1008}").text
    assert "Open, no receiving" in page and "Receive now" not in page
    assert (
        "PO-1008"
        not in client.get("/receiving").text.split('<section aria-labelledby="done-title">')[0]
    )
    refused = _deliver(client, "1")
    assert refused.status_code == 400
    assert "matched to the order only" in refused.text


def test_new_po_needs_receiving_unless_unticked(client: TestClient) -> None:
    vendor = str(seed_id("vendor", "clearpath"))
    for number, extra in (("PO-7001", {"needs_receiving": "1"}), ("PO-7002", {})):
        client.post(
            "/purchase-orders",
            data={
                "po_number": number,
                "vendor_id": vendor,
                "issued_date": "2026-10-01",
                "lines": "Helpdesk hours | 10 | 95.00",
                "person": "Sam",
                **extra,
            },
        )
    waiting = client.get("/receiving").text.split('<section aria-labelledby="done-title">')[0]

    assert "PO-7001" in waiting  # ticked: goods, waits for a delivery
    assert "PO-7002" not in waiting  # unticked: a service


def test_po_import_reads_needs_receiving(client: TestClient) -> None:
    csv = (
        "po_number,vendor,issued_date,currency,description,quantity,unit_price,gl_account,"
        "needs_receiving\n"
        "PO-8001,Clearpath IT Support LLC,2026-10-01,USD,Network audit,1,1800.00,6020,no\n"
        "PO-8002,Acme Supplies Inc,2026-10-01,USD,Toner cartridge,4,89.50,6000,\n"
    )
    client.post(
        "/purchase-orders/import",
        files={"file": ("pos.csv", csv.encode(), "text/csv")},
        data={"person": "Sam"},
    )
    waiting = client.get("/receiving").text.split('<section aria-labelledby="done-title">')[0]

    assert "PO-8002" in waiting and "PO-8001" not in waiting
