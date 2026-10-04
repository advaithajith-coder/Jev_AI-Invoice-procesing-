"""Recorded pipeline steps, the journey screen, and background processing."""

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


# ── Journey screen ────────────────────────────────────────────────────────────


def test_journey_page_shows_every_step_and_final_output(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app, seeded_settings, acme_invoice_fields(po_reference=None), jev=[wire_answers()]
    )
    invoice = _upload_api(client)

    page = client.get(f"/invoices/{invoice['id']}/journey").text

    for title in ("Received", "Read the file", "Vision transcription", "Extract fields (LLM)",
                  "Document checks", "Vendor and PO checks", "Jev judgment", "Decision", "Outcome"):  # fmt: skip
        assert title in page
    assert 'data-finished="true"' in page
    assert "Finished in" in page and "AI cost" in page
    assert "What the model read" in page
    assert "Bank account matches our records" in page
    assert "Decided by" in page and "No purchase order" in page
    assert "Final output" in page and "Waiting for a person." in page


def test_journey_while_processing(seeded_settings: Settings, tmp_path: Path) -> None:
    # An invoice that has only been received: the page shows waiting steps and keeps polling.
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
        page = client.get(f"/invoices/{invoice_id}/journey").text
        live = client.get(f"/invoices/{invoice_id}/journey/live").text

    assert 'data-finished="false"' in page
    assert "Processing" in page
    assert "Waiting for earlier steps." in page
    assert live.lstrip().startswith('<article class="slip journey"')
    assert "<html" not in live


def test_web_upload_redirects_to_journey(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])

    response = client.post(
        "/upload",
        files={"file": ("a.txt", ACME_INVOICE_TEXT.encode(), "text/plain")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/journey")
    assert "Approved automatically" in client.get(response.headers["location"]).text


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


def test_journey_is_not_finished_while_later_steps_run(seeded_settings: Settings) -> None:
    invoice_id = _stuck_after_extraction(seeded_settings)

    with TestClient(create_app(seeded_settings)) as client:
        page = client.get(f"/invoices/{invoice_id}/journey").text

    assert 'data-finished="false"' in page
    assert "track__step--running" in page
    assert "Did not run." not in page  # decision and outcome are waiting, not skipped


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
