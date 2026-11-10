"""Incomplete reading must not become a payable; only local fakes are used."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from jev_invoice.config import Settings
from jev_invoice.ingest.parsing import parse_document
from tests.fakes import (
    ACME_INVOICE_TEXT,
    acme_invoice_fields,
    install_fakes,
    make_image,
    wire_answers,
)


def test_mixed_pdf_keeps_page_order_and_only_reads_scanned_page(
    client: TestClient, app: FastAPI, seeded_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan = Image.new("RGB", (300, 400), "white")
    pages = []
    for text in (ACME_INVOICE_TEXT, "", "Final page: payment terms net 30 days."):
        page = MagicMock()
        page.width, page.height = 300, 400
        page.crop.return_value.extract_text.return_value = ""
        page.extract_text.return_value = text
        page.to_image.return_value = SimpleNamespace(original=scan)
        pages.append(page)
    pdf = MagicMock()
    pdf.__enter__.return_value.pages = pages
    monkeypatch.setattr("jev_invoice.ingest.parsing.pdfplumber.open", lambda _: pdf)
    llm, _ = install_fakes(
        app,
        seeded_settings,
        acme_invoice_fields(),
        jev=[wire_answers()],
        vision=["Second page: delivery reference DEL-20431"],
    )
    response = client.post(
        "/api/v1/invoices", files={"file": ("mixed.pdf", b"%PDF-mixed", "application/pdf")}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "approved", body
    prompt = llm.calls[0]["user"]
    assert (
        prompt.index("--- Page 1 ---")
        < prompt.index("--- Page 2 ---")
        < prompt.index("--- Page 3 ---")
    )
    assert "DEL-20431" in prompt
    pages[0].to_image.assert_not_called()
    pages[2].to_image.assert_not_called()
    assert body["additional_info"]["document_coverage"]["processed_pages"] == 3
    measurements = client.get(f"/api/v1/evaluations/measurements/invoices/{body['id']}").json()
    calls = measurements["attempts"][0]["calls"]
    assert {row["stage"] for row in calls} >= {"vision", "extraction", "judgment"}


def test_page_cap_creates_review_without_calling_models(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    llm, jev = install_fakes(app, seeded_settings)
    data = make_image((300, 400), fmt="TIFF", pages=7)
    parsed = parse_document(data, "pages.tif", max_pages=5)
    assert parsed.omitted_pages == (6, 7)
    body = client.post("/api/v1/invoices", files={"file": ("pages.tif", data, "image/tiff")}).json()
    assert body["status"] == "pending_review", body
    assert body["decision"]["reason_codes"] == ["INCOMPLETE_DOCUMENT"]
    assert not llm.calls and not jev.requests
    [task] = client.get("/api/v1/review").json()
    refused = client.post(f"/api/v1/review/{task['id']}/approve", json={"reviewer": "tester"})
    assert refused.status_code == 422
    assert client.get("/review").status_code == 200


def test_long_text_is_held_without_silent_clipping(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    llm, jev = install_fakes(app, seeded_settings)
    body = client.post(
        "/api/v1/invoices", files={"file": ("long.txt", b"x" * 60001, "text/plain")}
    ).json()
    assert body["status"] == "pending_review", body
    assert body["additional_info"]["document_coverage"]["document_characters"] == 60001
    assert not llm.calls and not jev.requests


def test_partially_unreadable_scan_is_held_before_extraction(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    llm, _ = install_fakes(app, seeded_settings, vision=[ACME_INVOICE_TEXT, "NO_TEXT"])
    data = make_image((300, 400), fmt="TIFF", pages=2)
    body = client.post("/api/v1/invoices", files={"file": ("scan.tif", data, "image/tiff")}).json()
    assert body["status"] == "pending_review", body
    assert body["additional_info"]["document_coverage"]["unreadable_pages"] == [2]
    assert not llm.calls
