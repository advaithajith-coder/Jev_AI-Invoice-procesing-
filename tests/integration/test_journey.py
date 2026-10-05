"""Recorded pipeline steps, the steps on the invoice page, and background processing."""

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from jev_invoice.config import Settings
from jev_invoice.db.models import Invoice, PipelineStep
from jev_invoice.db.session import create_engine, create_session_factory
from jev_invoice.decision.service import DecisionService
from jev_invoice.domain.enums import InvoiceStatus
from jev_invoice.extraction.client import LLMClient
from jev_invoice.extraction.service import ExtractionService
from jev_invoice.ingest.storage import LocalFileStorage
from jev_invoice.main import create_app
from jev_invoice.pipeline.runner import BackgroundRunner
from jev_invoice.pipeline.service import InvoicePipeline
from tests.conftest import create_schema, make_settings
from tests.fakes import (
    ACME_INVOICE_TEXT,
    FakeJevServer,
    FakeLLMProvider,
    acme_invoice_fields,
    error_response,
    install_fakes,
    make_image,
    make_jev_client,
    wire_answers,
)


def _steps(settings: Settings, invoice_id: str) -> dict[str, PipelineStep]:
    async def load() -> dict[str, PipelineStep]:
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session:
                rows = await session.scalars(
                    select(PipelineStep).where(PipelineStep.invoice_id == uuid.UUID(invoice_id))
                )
                return {row.step: row for row in rows}
        finally:
            await engine.dispose()

    return asyncio.run(load())


def _upload_api(
    client: TestClient,
    content: bytes = ACME_INVOICE_TEXT.encode(),
    name: str = "a.txt",
    mime: str = "text/plain",
) -> dict[str, Any]:
    body: dict[str, Any] = client.post(
        "/api/v1/invoices", files={"file": (name, content, mime)}
    ).json()
    return body


# ── Recorded steps ────────────────────────────────────────────────────────────


def test_every_step_is_recorded_with_details(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])

    invoice = _upload_api(client)
    steps = _steps(seeded_settings, invoice["id"])

    assert list(sorted(steps, key=lambda k: steps[k].position)) == [
        "received", "read", "transcribe", "extract", "document_checks",
        "master_data", "judgment", "coding", "decision", "outcome",
    ]  # fmt: skip
    assert steps["transcribe"].status == "skipped"
    assert all(
        steps[k].status == "done"
        for k in ("received", "read", "extract", "judgment", "decision", "outcome")
    )
    assert all(steps[k].duration_ms is not None for k in steps)

    extract = steps["extract"].details
    assert extract["model"] == "fake-model"
    assert extract["fields"]["vendor_name"] == "Acme Supplies Inc"
    assert extract["bank_account_last4"] == "6819"
    assert "bank_account" not in extract["fields"]
    assert "You read supplier invoices" in extract["instructions"]

    checks = {c["name"]: c for c in steps["master_data"].details["checks"]}
    assert checks["bank"]["passed"] is True
    assert checks["po"]["detail"].startswith("PO-1003")
    assert checks["line_received"]["passed"] is True  # line-by-line 3-way match
    assert checks["line_prices"]["passed"] is True

    assert steps["judgment"].details["answers"]["document_type"] == "invoice"
    assert set(steps["judgment"].details["questions"]) >= {"document_type", "bank_details_change"}
    assert steps["decision"].details["action"] == "auto_approve"
    assert steps["decision"].details["deciding_reasons"] == []
    assert steps["outcome"].summary.startswith("Approved automatically")


def test_failed_checks_mark_step_as_needing_attention(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app,
        seeded_settings,
        acme_invoice_fields(bank_account="GB11 FAKE 0400 7519 2837 46"),
        jev=[wire_answers()],
    )

    steps = _steps(seeded_settings, _upload_api(client)["id"])

    assert steps["master_data"].status == "warning"
    assert steps["decision"].details["deciding_reasons"] == ["BANK_ACCOUNT_MISMATCH"]
    assert steps["outcome"].summary.startswith("Sent to the review queue")


def test_extraction_failure_stops_the_pipeline_cleanly(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, "not json")

    steps = _steps(seeded_settings, _upload_api(client)["id"])

    assert steps["extract"].status == "failed"
    assert "could not be used" in steps["extract"].summary
    assert steps["outcome"].status == "failed"
    assert "document_checks" not in steps and "judgment" not in steps


def test_jev_outage_shows_as_warning_not_failure(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[error_response(500)])

    steps = _steps(seeded_settings, _upload_api(client)["id"])

    assert steps["judgment"].status == "warning"
    assert "did not answer" in steps["judgment"].summary
    assert steps["outcome"].status == "done"


def test_photo_records_transcription_step(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app,
        seeded_settings,
        acme_invoice_fields(),
        jev=[wire_answers()],
        vision=[ACME_INVOICE_TEXT],
    )

    steps = _steps(
        seeded_settings, _upload_api(client, make_image(fmt="JPEG"), "p.jpg", "image/jpeg")["id"]
    )

    assert steps["read"].details["needs_transcription"] is True
    assert steps["transcribe"].status == "done"
    assert steps["transcribe"].details["text"].startswith("INVOICE AS-20431")


# ── Steps on the invoice page ─────────────────────────────────────────────────


def test_invoice_page_shows_each_steps_output(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app, seeded_settings, acme_invoice_fields(po_reference=None), jev=[wire_answers()]
    )
    invoice = _upload_api(client)

    page = client.get(f"/invoices/{invoice['id']}").text

    for title in ("Received", "Read the file", "Vision transcription", "Extract fields (LLM)",
                  "Document checks", "Vendor and PO checks", "Jev judgment", "Account coding",
                  "Decision", "Outcome"):  # fmt: skip
        assert title in page
    assert 'data-finished="true"' in page
    assert "Finished in" in page
    assert "AS-20431 · 30 Sep 2026 · no PO · 1 line · USD 1,074.00" in page
    assert "Not needed: the file has text" in page
    assert "Bank account matches our records" in page  # the full checklist opens from the row
    assert "Invoice, asks for payment, no risk signals" in page
    assert "Needs budget approval: No purchase order" in page
    assert "In the review queue" in page


def test_invoice_page_leaves_prompts_and_rules_to_the_workflow_page(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])
    invoice = _upload_api(client)

    page = client.get(f"/invoices/{invoice['id']}").text
    workflow = client.get("/workflow").text

    for detail in ("You read supplier invoices", "fake-model", "Questions sent to Jev"):
        assert detail not in page
    assert 'data-info="extract"' in workflow  # the (i) on the step opens its dialog
    assert 'id="info-extract"' in workflow
    assert "You read supplier invoices" in workflow  # the extraction prompt
    assert "Which account in the chart of accounts" in workflow  # Jev's coding question
    assert "at most 5,000.00 USD" in workflow


def test_invoice_page_while_processing(seeded_settings: Settings, tmp_path: Path) -> None:
    # An invoice that has only been received: its steps wait, and the page keeps polling.
    async def receive_only() -> str:
        engine = create_engine(seeded_settings)
        try:
            async with create_session_factory(engine)() as session:
                pipeline = InvoicePipeline(
                    session,
                    seeded_settings,
                    storage=LocalFileStorage(seeded_settings.storage_dir),
                    extraction=None,
                    decisions=DecisionService(None, seeded_settings),
                )
                result = await pipeline.receive(ACME_INVOICE_TEXT.encode(), "a.txt", "text/plain")
                return str(result.invoice_id)
        finally:
            await engine.dispose()

    invoice_id = asyncio.run(receive_only())
    with TestClient(create_app(seeded_settings)) as client:
        page = client.get(f"/invoices/{invoice_id}").text
        live = client.get(f"/invoices/{invoice_id}/steps").text

    assert 'data-finished="false"' in page
    assert 'data-live-url="/invoices/' in page
    assert "Waiting for the steps before it" in page
    assert live.lstrip().startswith('<section class="steps"')
    assert "<html" not in live


def test_web_upload_opens_the_invoice_page(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])

    response = client.post(
        "/upload",
        files={"file": ("a.txt", ACME_INVOICE_TEXT.encode(), "text/plain")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/invoices/") and location.count("/") == 2
    assert "Approved automatically" in client.get(location).text


def test_old_journey_links_open_the_invoice_page(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])
    invoice_id = _upload_api(client)["id"]

    response = client.get(f"/invoices/{invoice_id}/journey", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == f"/invoices/{invoice_id}"


# ── Background processing ─────────────────────────────────────────────────────


def test_background_upload_returns_before_processing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, background_processing=True)
    asyncio.run(create_schema(settings))
    app = create_app(settings)

    with TestClient(app) as client:
        install_fakes(app, settings, acme_invoice_fields(), jev=[wire_answers()])
        response = client.post(
            "/upload",
            files={"file": ("a.txt", ACME_INVOICE_TEXT.encode(), "text/plain")},
            follow_redirects=False,
        )
        invoice_id = response.headers["location"].split("/")[2]
        client.portal.call(app.state.runner.wait_idle)  # type: ignore[union-attr]
        body = client.get(f"/api/v1/invoices/{invoice_id}").json()

    assert response.status_code == 303
    assert body["status"] == "approved"


def test_runner_resumes_invoices_left_unprocessed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    asyncio.run(create_schema(settings))

    async def scenario() -> str:
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        storage = LocalFileStorage(settings.storage_dir)

        def build(session: Any) -> InvoicePipeline:
            return InvoicePipeline(
                session,
                settings,
                storage=storage,
                extraction=ExtractionService(
                    LLMClient(FakeLLMProvider(acme_invoice_fields()), settings)
                ),
                decisions=DecisionService(
                    make_jev_client(FakeJevServer(wire_answers()), settings), settings
                ),
            )

        try:
            async with factory() as session:  # simulate a restart after receiving
                received = await build(session).receive(
                    ACME_INVOICE_TEXT.encode(), "a.txt", "text/plain"
                )
            runner = BackgroundRunner(factory, build)
            assert await runner.resume_pending() == 1
            await runner.wait_idle()
            async with factory() as session:
                invoice = await session.get(Invoice, received.invoice_id)
                assert invoice is not None
                return str(invoice.status)
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == InvoiceStatus.APPROVED


def _stuck_after_extraction(settings: Settings) -> uuid.UUID:
    """An invoice interrupted mid-processing: extracted, with Jev's step still 'running'."""

    async def make() -> uuid.UUID:
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session:
                pipeline = InvoicePipeline(
                    session,
                    settings,
                    storage=LocalFileStorage(settings.storage_dir),
                    extraction=None,
                    decisions=DecisionService(None, settings),
                )
                received = await pipeline.receive(ACME_INVOICE_TEXT.encode(), "a.txt", "text/plain")
                invoice = await session.get(Invoice, received.invoice_id)
                assert invoice is not None
                invoice.status = InvoiceStatus.EXTRACTED
                now = datetime.now(UTC)
                for step, position, status in (("extract", 4, "done"), ("judgment", 7, "running")):
                    session.add(
                        PipelineStep(
                            invoice_id=invoice.id, step=step, position=position, status=status,
                            started_at=now, summary="", details={},
                        )
                    )  # fmt: skip
                await session.commit()
                return received.invoice_id
        finally:
            await engine.dispose()

    return asyncio.run(make())


def test_steps_are_not_finished_while_later_steps_run(seeded_settings: Settings) -> None:
    invoice_id = _stuck_after_extraction(seeded_settings)

    with TestClient(create_app(seeded_settings)) as client:
        page = client.get(f"/invoices/{invoice_id}").text

    assert 'data-finished="false"' in page
    assert "step--running" in page
    assert "Did not run" not in page  # decision and outcome are waiting, not skipped


def test_runner_restarts_an_invoice_interrupted_after_extraction(seeded_settings: Settings) -> None:
    invoice_id = _stuck_after_extraction(seeded_settings)

    async def resume() -> tuple[str, list[str]]:
        engine = create_engine(seeded_settings)
        factory = create_session_factory(engine)
        storage = LocalFileStorage(seeded_settings.storage_dir)

        def build(session: Any) -> InvoicePipeline:
            return InvoicePipeline(
                session,
                seeded_settings,
                storage=storage,
                extraction=ExtractionService(
                    LLMClient(FakeLLMProvider(acme_invoice_fields()), seeded_settings)
                ),
                decisions=DecisionService(
                    make_jev_client(FakeJevServer(wire_answers()), seeded_settings), seeded_settings
                ),
            )

        try:
            runner = BackgroundRunner(factory, build)
            assert await runner.resume_pending() == 1
            await runner.wait_idle()
            async with factory() as session:
                invoice = await session.get(Invoice, invoice_id)
                steps = await session.scalars(
                    select(PipelineStep.status).where(PipelineStep.invoice_id == invoice_id)
                )
                assert invoice is not None
                return str(invoice.status), list(steps)
        finally:
            await engine.dispose()

    status, step_statuses = asyncio.run(resume())

    assert status == InvoiceStatus.APPROVED
    assert "running" not in step_statuses
    assert len(step_statuses) == 10  # nine desks plus account coding
