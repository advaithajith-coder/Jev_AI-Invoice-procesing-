"""End to end through the HTTP API: upload -> pipeline -> database -> review.

Groq and Jev are faked (the Jev fake runs the real TypeSafe SDK); the database is a
real, seeded SQLite file per test.
"""

import asyncio
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from jev_invoice.config import Settings
from jev_invoice.db.models import AuditEvent
from jev_invoice.db.session import create_engine, create_session_factory
from jev_invoice.decision.service import DecisionService
from jev_invoice.extraction.client import LLMClient
from jev_invoice.extraction.service import ExtractionService
from tests.fakes import (
    ACME_INVOICE_TEXT,
    FakeJevServer,
    FakeLLMProvider,
    acme_invoice_fields,
    error_response,
    install_fakes,
    make_email,
    make_image,
    make_jev_client,
    make_pdf,
    wire_answers,
)


class Pipeline:
    """Scripted fakes installed into a running app."""

    def __init__(self, app: FastAPI, settings: Settings) -> None:
        self.app, self.settings = app, settings
        self.llm = FakeLLMProvider()
        self.jev = FakeJevServer()

    def script(
        self, *llm: Any, jev: list[Any] | None = None, jev_configured: bool = True
    ) -> "Pipeline":
        self.llm = FakeLLMProvider(*llm)
        self.jev = FakeJevServer(*(jev or []))
        self.app.state.extraction = ExtractionService(LLMClient(self.llm, self.settings))
        jev_client = make_jev_client(self.jev, self.settings) if jev_configured else None
        self.app.state.decisions = DecisionService(jev_client, self.settings)
        return self


@pytest.fixture
def pipeline(app: FastAPI, client: TestClient, seeded_settings: Settings) -> Pipeline:
    return Pipeline(app, seeded_settings)


def _upload(
    client: TestClient,
    content: bytes = ACME_INVOICE_TEXT.encode(),
    name: str = "AS-20431.txt",
    content_type: str = "text/plain",
) -> Any:
    return client.post("/api/v1/invoices", files={"file": (name, content, content_type)})


def _audit_actions(settings: Settings, invoice_id: str) -> list[str]:
    async def load() -> list[str]:
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session:
                rows = await session.scalars(select(AuditEvent.action).order_by(AuditEvent.id))
                return list(rows)
        finally:
            await engine.dispose()

    return asyncio.run(load())


# ── Happy path ────────────────────────────────────────────────────────────────


def test_clean_invoice_is_auto_approved(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(acme_invoice_fields(), jev=[wire_answers()])

    response = _upload(client)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["decision"]["action"] == "auto_approve"
    assert body["decision"]["reason_codes"] == []
    assert body["decision"]["jev_status"] == "ok"
    assert body["vendor_matched"] is True
    assert body["purchase_order_number"] == "PO-1003"
    assert body["total"] == "1074.00"
    assert body["bank_account_last4"] == "6819"
    assert body["lines"][0]["description"].startswith("Toner")
    assert body["extraction"]["prompt_version"] == "x5"
    assert client.get("/api/v1/review").json() == []


def test_raw_bank_account_never_leaves_the_api(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(acme_invoice_fields(), jev=[wire_answers()])

    text = _upload(client).text

    assert "6016 1331" not in text and "60161331" not in text


def test_same_file_twice_is_processed_once(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(acme_invoice_fields(), jev=[wire_answers()])
    first = _upload(client).json()

    second = _upload(client, name="renamed.txt")

    assert second.status_code == 200
    assert second.json()["already_received"] is True
    assert second.json()["id"] == first["id"]
    assert len(pipeline.llm.calls) == 1 and len(pipeline.jev.requests) == 1


def test_pdf_and_email_uploads(client: TestClient, pipeline: Pipeline) -> None:
    pdf = make_pdf(ACME_INVOICE_TEXT.splitlines())
    pipeline.script(
        acme_invoice_fields(),
        acme_invoice_fields(invoice_number="AS-20432"),
        jev=[wire_answers(), wire_answers()],
    )

    as_pdf = _upload(client, pdf, "AS-20431.pdf", "application/pdf").json()
    as_email = _upload(
        client, make_email(pdf=make_pdf(["INVOICE AS-20432"])), "mail.eml", "message/rfc822"
    ).json()

    assert as_pdf["source"] == "upload" and as_pdf["status"] == "approved"
    assert as_email["source"] == "email"
    assert "AS-20431" in pipeline.llm.calls[0]["user"]


# ── Routing to review ─────────────────────────────────────────────────────────


def test_changed_bank_account_goes_to_fraud_review(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(
        acme_invoice_fields(bank_account="GB11 FAKE 0400 7519 2837 46"), jev=[wire_answers()]
    )

    body = _upload(client).json()

    assert body["status"] == "pending_review"
    assert (body["decision"]["action"], body["decision"]["band"]) == ("fraud_review", "block")
    [finding] = body["decision"]["findings"]
    assert finding["code"] == "BANK_ACCOUNT_MISMATCH"
    [task] = client.get("/api/v1/review").json()
    assert task["invoice"]["id"] == body["id"]
    assert task["invoice"]["decision"]["reason_codes"] == ["BANK_ACCOUNT_MISMATCH"]


def test_jev_fraud_signal_blocks_even_with_clean_data(
    client: TestClient, pipeline: Pipeline
) -> None:
    pipeline.script(acme_invoice_fields(), jev=[wire_answers(bank_details_change=0.93)])

    decision = _upload(client).json()["decision"]

    assert decision["action"] == "fraud_review"
    assert decision["reason_codes"] == ["BANK_DETAILS_CHANGED"]


def test_no_purchase_order_routes_for_approval(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(acme_invoice_fields(po_reference=None), jev=[wire_answers()])

    decision = _upload(client).json()["decision"]

    assert (decision["action"], decision["band"]) == ("route_approval", "confirm")


def test_without_jev_every_invoice_goes_to_review(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(acme_invoice_fields(), jev_configured=False)

    body = _upload(client).json()

    assert body["decision"]["reason_codes"] == ["JEV_NOT_CONFIGURED"]
    assert body["status"] == "pending_review"


def test_jev_outage_fails_closed(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(acme_invoice_fields(), jev=[error_response(500)])

    body = _upload(client).json()

    assert body["decision"]["action"] == "hold_review"
    assert body["decision"]["reason_codes"] == ["JEV_UNAVAILABLE"]


# ── Failures ──────────────────────────────────────────────────────────────────


def test_unusable_extraction_marks_invoice_failed(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script("this is not json")

    body = _upload(client).json()

    assert body["status"] == "failed"
    assert body["decision"] is None
    assert "could not be used" in body["error_message"]
    assert pipeline.jev.requests == []


def test_extraction_not_configured(client: TestClient, app: FastAPI) -> None:
    app.state.extraction = None

    body = _upload(client).json()

    assert body["status"] == "failed"
    assert "GROQ_API_KEY" in body["error_message"]


def test_unsupported_file_rejected(client: TestClient) -> None:
    response = _upload(client, b"PK\x03\x04", "invoice.docx", "application/octet-stream")

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_document"


def test_oversized_file_rejected(client: TestClient, app: FastAPI) -> None:
    app.state.settings = app.state.settings.model_copy(update={"max_upload_bytes": 10})

    response = _upload(client)

    assert response.status_code == 413


# ── Review workflow ───────────────────────────────────────────────────────────


def test_reviewer_approves_and_audit_trail_is_complete(
    client: TestClient, pipeline: Pipeline, seeded_settings: Settings
) -> None:
    pipeline.script(acme_invoice_fields(po_reference=None), jev=[wire_answers()])
    invoice_id = _upload(client).json()["id"]
    [task] = client.get("/api/v1/review").json()

    response = client.post(
        f"/api/v1/review/{task['id']}/approve", json={"reviewer": "maya", "note": "Budget OK"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["resolved_by"] == "maya"
    assert client.get(f"/api/v1/invoices/{invoice_id}").json()["status"] == "approved"
    assert client.get("/api/v1/review").json() == []
    assert _audit_actions(seeded_settings, invoice_id) == [
        "invoice.received",
        "invoice.extracted",
        "decision.made",
        "review.opened",
        "review.approved",
    ]


def test_reviewer_rejects(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(acme_invoice_fields(po_reference=None), jev=[wire_answers()])
    invoice_id = _upload(client).json()["id"]
    [task] = client.get("/api/v1/review").json()

    client.post(f"/api/v1/review/{task['id']}/reject", json={"reviewer": "maya"})

    assert client.get(f"/api/v1/invoices/{invoice_id}").json()["status"] == "rejected"


def test_task_cannot_be_resolved_twice(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(acme_invoice_fields(po_reference=None), jev=[wire_answers()])
    _upload(client)
    [task] = client.get("/api/v1/review").json()
    client.post(f"/api/v1/review/{task['id']}/approve", json={"reviewer": "a"})

    again = client.post(f"/api/v1/review/{task['id']}/reject", json={"reviewer": "b"})

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "conflict"


def test_reviewer_name_required(client: TestClient) -> None:
    response = client.post(f"/api/v1/review/{uuid.uuid4()}/approve", json={"reviewer": ""})
    assert response.status_code == 422


def test_unknown_review_task(client: TestClient) -> None:
    response = client.post(f"/api/v1/review/{uuid.uuid4()}/approve", json={"reviewer": "a"})
    assert response.status_code == 404


# ── Listing ───────────────────────────────────────────────────────────────────


def test_list_and_filter_invoices(client: TestClient, pipeline: Pipeline) -> None:
    pipeline.script(
        acme_invoice_fields(),
        acme_invoice_fields(invoice_number="AS-20999", po_reference=None),
        jev=[wire_answers(), wire_answers()],
    )
    _upload(client)
    _upload(client, (ACME_INVOICE_TEXT + "\nsecond").encode(), "second.txt")

    everything = client.get("/api/v1/invoices").json()
    approved = client.get("/api/v1/invoices", params={"status": "approved"}).json()

    assert len(everything) == 2
    assert [invoice["status"] for invoice in approved] == ["approved"]
    assert client.get(f"/api/v1/invoices/{uuid.uuid4()}").status_code == 404


def test_blocked_invoice_approval_needs_note_via_api(
    client: TestClient, pipeline: Pipeline
) -> None:
    pipeline.script(
        acme_invoice_fields(bank_account="GB11 FAKE 0400 7519 2837 46"), jev=[wire_answers()]
    )
    _upload(client)
    [task] = client.get("/api/v1/review").json()

    refused = client.post(f"/api/v1/review/{task['id']}/approve", json={"reviewer": "maya"})
    accepted = client.post(
        f"/api/v1/review/{task['id']}/approve",
        json={"reviewer": "maya", "note": "Verified by phone on the number in our records"},
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "note_required"
    assert accepted.status_code == 200


# ── Scans and photos ──────────────────────────────────────────────────────────


def test_photo_is_transcribed_then_follows_the_normal_workflow(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    llm, jev = install_fakes(
        app,
        seeded_settings,
        acme_invoice_fields(),
        jev=[wire_answers()],
        vision=[ACME_INVOICE_TEXT],
    )

    response = client.post(
        "/api/v1/invoices", files={"file": ("photo.jpg", make_image(fmt="JPEG"), "image/jpeg")}
    )

    body = response.json()
    assert response.status_code == 201, body
    assert body["status"] == "approved"
    assert body["transcription"]["text"].startswith("INVOICE AS-20431")
    assert "INVOICE AS-20431" in llm.calls[0]["user"]  # extraction read the transcription
    assert jev.requests[0]["state"] == ACME_INVOICE_TEXT.strip()  # and so did Jev
    assert "invoice.transcribed" in _audit_actions(seeded_settings, body["id"])


def test_scan_without_vision_model_fails_clearly(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, jev=[wire_answers()], vision=None)

    body = client.post(
        "/api/v1/invoices", files={"file": ("scan.pdf", make_pdf([]), "application/pdf")}
    ).json()

    assert body["status"] == "failed"
    assert "VISION_MODEL" in body["error_message"]


def test_photo_with_no_text_fails_clearly(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, vision=["NO_TEXT"])

    body = client.post(
        "/api/v1/invoices", files={"file": ("cat.png", make_image(), "image/png")}
    ).json()

    assert body["status"] == "failed"
    assert "No readable text" in body["error_message"]
    assert body["decision"] is None
