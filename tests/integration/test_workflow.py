"""The live workflow map: the page and the numbers behind it."""

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from tests.fakes import ACME_INVOICE_TEXT, acme_invoice_fields, install_fakes, wire_answers


def _live(client: TestClient) -> dict[str, Any]:
    response = client.get("/workflow/live")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


def test_workflow_page_draws_every_desk_and_branch(client: TestClient) -> None:
    page = client.get("/workflow").text

    for desk in ("arrive", "read", "vision", "extract", "doc", "md", "jev", "policy",
                 "review", "person", "approved", "export", "accounting", "rejected", "failed"):  # fmt: skip
        assert f'data-node="{desk}"' in page
    for branch in ("e-read_vision", "e-read_extract", "e-policy_approved", "e-policy_review",
                   "e-person_rejected", "e-extract_failed"):  # fmt: skip
        assert f'id="{branch}"' in page
    assert 'aria-current="page">Workflow' in page
    assert "Pause movement" in page
    assert "Nothing on the route." in page
    assert "not real invoices" in page  # the sample slips are labelled as a sample


def test_live_data_with_no_invoices_uses_default_splits(client: TestClient) -> None:
    live = _live(client)

    assert live["sample"] == 0
    assert live["in_flight"] == [] and live["recent"] == []
    assert live["split"] == {"scan": 0.3, "failed": 0.05, "auto": 0.35, "review_approved": 0.7}


def test_live_data_follows_real_invoices(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])
    invoice = client.post(
        "/api/v1/invoices", files={"file": ("a.txt", ACME_INVOICE_TEXT.encode(), "text/plain")}
    ).json()
    assert invoice["status"] == "approved"

    live = _live(client)

    assert live["sample"] == 1
    assert live["counts"]["approved"] == 1 and live["counts"]["in_flight"] == 0
    assert live["split"]["scan"] == 0.0  # a text file skipped the vision desk
    assert live["split"]["auto"] == 1.0
    assert live["avg_ms"]["extract"] >= 0
    assert live["recent"][0]["number"] == "AS-20431"
    assert live["recent"][0]["url"] == f"/invoices/{invoice['id']}/journey"
    page = client.get("/workflow").text
    assert "1 now" in page  # the Approved desk shows its count
    assert "AS-20431" in page


def test_invoice_names_are_text_not_markup(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app,
        seeded_settings,
        acme_invoice_fields(vendor_name="<img src=x onerror=alert(1)>"),
        jev=[wire_answers()],
    )
    client.post("/api/v1/invoices", files={"file": ("a.txt", b"INVOICE X-1", "text/plain")})

    page = client.get("/workflow").text

    assert "<img src=x" not in page
