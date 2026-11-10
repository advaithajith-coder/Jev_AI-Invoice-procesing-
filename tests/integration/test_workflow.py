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
                 "review", "person", "approved", "export", "rejected", "failed"):  # fmt: skip
        assert f'data-node="{desk}"' in page
    for branch in ("e-read_vision", "e-read_extract", "e-policy_approved", "e-policy_review",
                   "e-person_rejected", "e-extract_failed"):  # fmt: skip
        assert f'id="{branch}"' in page
    assert 'aria-current="page">Live map' in page
    assert "Pause movement" not in page
    assert '<p class="wf-upload-prompt"><a href="/inbox">Upload Invoice</a></p>' in page
    assert "wf-panels" not in page


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
    assert live["recent"][0]["url"] == f"/invoices/{invoice['id']}"
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


def test_root_is_a_simple_decision_tree_and_inbox_is_separate(client: TestClient) -> None:
    page = client.get("/").text
    assert "Parser.AI" in page
    for label in ("Total invoices", "Checks passed", "Review required", "Stopped"):
        assert label in page
    assert 'aria-current="page">Overview' in page
    assert 'href="/workflow"' in page and 'href="/workflow/pipeline"' in page
    assert "<svg viewBox" not in page  # the moving map lives on /workflow
    assert 'href="/inbox"' in page
    assert 'aria-controls="workspace-navigation"' in page


def test_every_step_page_hands_the_route_to_react(client: TestClient) -> None:
    page = client.get("/workflow/pipeline").text
    assert '<div class="react-pipeline">' in page
    assert 'id="workflow-data"' in page
    assert 'aria-current="page">Every step' in page


def test_inbox_has_the_upload_form(client: TestClient) -> None:
    assert 'action="/upload"' in client.get("/inbox").text
