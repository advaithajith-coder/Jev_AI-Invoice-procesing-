import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from jev_invoice.config import Settings
from jev_invoice.db.seed import seed_id
from tests.integration.test_payables import _approve


def report(
    client: TestClient, po: str = "PO-1003", quantity: str = "1", category: str = "damaged"
) -> Response:
    return client.post(
        "/receiving/issues/report",
        data={
            "po_id": str(seed_id("po", po)),
            "line_id": str(seed_id("po_line", f"{po}:1")),
            "quantity": quantity,
            "category": category,
            "reason": "Damaged packaging",
            "person": "Sam",
        },
        follow_redirects=False,
    )


def issue_id(client: TestClient) -> str:
    page = client.get("/receiving").text
    match = re.search(r"/receiving/issues/([0-9a-f-]+)/dispute", page)
    assert match is not None
    return match.group(1)


def test_report_and_duplicate(client: TestClient) -> None:
    assert report(client).status_code == 303
    page = client.get("/receiving").text
    assert "Damaged packaging" in page and "Estimated credit before tax" in page
    assert report(client).status_code == 400


def test_invalid_quantities_and_service(client: TestClient) -> None:
    for qty in ("0", "-1", "NaN", "51", "abc"):
        assert report(client, quantity=qty).status_code == 400
    assert report(client, po="PO-1004").status_code == 400
    assert report(client, po="PO-1002", quantity="2", category="not_received").status_code == 400
    assert report(client, po="PO-1002", category="not_received").status_code == 303


def test_dispute_and_credit_lifecycle(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    invoice = _approve(app, client, seeded_settings, invoice_number="ISSUE-1")
    assert report(client).status_code == 303
    issue = issue_id(client)
    data = {"invoice_id": invoice, "person": "Sam"}
    assert (
        client.post(
            f"/receiving/issues/{issue}/dispute", data=data, follow_redirects=False
        ).status_code
        == 303
    )
    page = client.get(f"/invoices/{invoice}").text
    assert "Damaged packaging" in page
    invalid = {**data, "credit_number": "CN-1", "credit_amount": "999999"}
    assert client.post(f"/receiving/issues/{issue}/credit", data=invalid).status_code == 400
    valid = {**data, "credit_number": "CN-1", "credit_amount": "24.99"}
    assert (
        client.post(
            f"/receiving/issues/{issue}/credit", data=valid, follow_redirects=False
        ).status_code
        == 303
    )
    page = client.get("/receiving").text
    assert "Credit applied: CN-1" in page
    assert client.post(f"/receiving/issues/{issue}/credit", data=valid).status_code == 400
    page = client.get(f"/invoices/{invoice}").text
    assert "CN-1" in page and "1,049.01" in page
