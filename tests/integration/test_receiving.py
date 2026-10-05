"""Receiving: record deliveries, and invoices waiting for goods are checked again."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from jev_invoice.db.seed import seed_id
from tests.fakes import acme_invoice_fields, install_fakes, wire_answers

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
    waiting, recent = page.split("Recent deliveries", 1)

    assert "PO-1008" in waiting and "Meridian Legal LLP" in waiting
    assert "PO-1005" in waiting  # ordered, nothing received yet
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

    response = _deliver(client, "4")

    assert response.status_code == 303
    assert "approved=ML-412" in response.headers["location"]
    after = client.get(f"/api/v1/invoices/{body['id']}").json()
    assert after["status"] == "approved"
    assert after["decision"]["reason_codes"] == []
    assert len(llm.calls) == 1 and len(jev.requests) == 1  # nothing asked again
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


def test_more_than_is_still_to_come_is_refused(client: TestClient) -> None:
    response = _deliver(client, "5")  # only 4 hours are still to come

    assert response.status_code == 400
    assert "only 4 is still outstanding" in response.text
