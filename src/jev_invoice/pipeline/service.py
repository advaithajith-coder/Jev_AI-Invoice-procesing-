"""Process one uploaded invoice from bytes to a stored, auditable decision.

Two phases, so the screen can follow an invoice while it is processed:

    receive  (fast, during the upload)
        received -> read the file
    process  (slow: model calls; in the background for screen uploads)
        [vision transcription] -> extract (LLM) -> document checks -> vendor and PO checks
        -> Jev judgment -> decision (policy) -> outcome (auto-approve or review queue)

Every step is recorded (pipeline_steps) as it starts and ends, with timings, model calls,
costs, inputs and outputs. Scans and photos become text first, then follow the same path.
The same file uploaded twice is recognised by its SHA-256 and processed only once.
"""

import hashlib
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import PurePath
from typing import Any

import structlog
from sqlalchemy import Select, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jev_invoice.coding.service import QUESTION_SET_VERSION as CODING_QUESTION_SET
from jev_invoice.coding.service import CodingService
from jev_invoice.config import Settings
from jev_invoice.core import audit
from jev_invoice.core.errors import AppError
from jev_invoice.db.models import (
    Decision,
    GLAccount,
    Invoice,
    InvoiceLine,
    PipelineStep,
    ReviewTask,
)
from jev_invoice.decision.policy import ACTION_PRIORITY, POLICY_VERSION
from jev_invoice.decision.questions import QUESTION_SET_VERSION, build_questions
from jev_invoice.decision.service import DecisionService, Judgment
from jev_invoice.domain.checks import CheckOutcome, run_document_checks
from jev_invoice.domain.decision import DecisionOutcome
from jev_invoice.domain.enums import ActionBand, DocumentType, InvoiceStatus, ReviewStatus
from jev_invoice.domain.vendors import bank_account_last4
from jev_invoice.extraction.prompts import SYSTEM_PROMPT
from jev_invoice.extraction.service import ExtractionResult, ExtractionService
from jev_invoice.extraction.vision import TranscriptionService
from jev_invoice.ingest.parsing import ParsedDocument, parse_document
from jev_invoice.ingest.storage import LocalFileStorage
from jev_invoice.masterdata.rules import RulesService
from jev_invoice.matching.service import MatchingService, MatchResult, normalize_reference
from jev_invoice.pipeline.steps import StepRecorder

log = structlog.get_logger(__name__)

_TEXT_PREVIEW_CHARS = 4000
SOURCE_WORDS = {
    "po": "from the PO",
    "ai": "chosen by Jev",
    "vendor": "vendor default",
    "suspense": "need coding",
}
RECEIVE_STEPS = ("received", "read")
# Statuses an invoice can be left in if processing is interrupted (resumed on startup).
IN_PROGRESS = (InvoiceStatus.RECEIVED, InvoiceStatus.EXTRACTED)


@dataclass(frozen=True)
class ProcessResult:
    invoice_id: uuid.UUID
    already_received: bool


def invoice_query() -> Select[Invoice]:
    """Invoice with everything the API and screens show, loaded eagerly."""
    return select(Invoice).options(
        selectinload(Invoice.lines),
        selectinload(Invoice.decisions),
        selectinload(Invoice.vendor),
        selectinload(Invoice.purchase_order),
        selectinload(Invoice.steps),
        selectinload(Invoice.payments),
        selectinload(Invoice.credits),
    )


class InvoicePipeline:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        storage: LocalFileStorage,
        extraction: ExtractionService | None,
        decisions: DecisionService,
        transcription: TranscriptionService | None = None,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage
        self._extraction = extraction
        self._decisions = decisions
        self._transcription = transcription
        self._today = today

    async def process_upload(
        self, data: bytes, filename: str, content_type: str | None
    ) -> ProcessResult:
        """Receive and process in one go (the JSON API and tests use this)."""
        received = await self.receive(data, filename, content_type)
        if not received.already_received:
            await self.process(received.invoice_id)
        return received

    # ── Phase 1: receive ──────────────────────────────────────────────────────

    async def receive(self, data: bytes, filename: str, content_type: str | None) -> ProcessResult:
        """Validate, de-duplicate, and store the file. Fast; no model calls."""
        parsed = self._parse(data, filename, content_type)
        existing_id = await self._session.scalar(
            select(Invoice.id).where(Invoice.file_sha256 == parsed.sha256)
        )
        if existing_id is not None:
            log.info("duplicate_upload", invoice_id=str(existing_id))
            return ProcessResult(existing_id, already_received=True)

        invoice = self._new_invoice(parsed, data)
        self._session.add(invoice)
        try:
            await self._session.flush()
        except IntegrityError:  # the same file is being uploaded concurrently
            await self._session.rollback()
            existing_id = await self._session.scalar(
                select(Invoice.id).where(Invoice.file_sha256 == parsed.sha256)
            )
            if existing_id is None:
                raise
            return ProcessResult(existing_id, already_received=True)
        structlog.contextvars.bind_contextvars(invoice_id=str(invoice.id))
        audit.record(
            self._session,
            action="invoice.received",
            entity_type="invoice",
            entity_id=invoice.id,
            data={"filename": filename, "source": parsed.source, "sha256": parsed.sha256},
        )

        steps = StepRecorder(self._session, invoice.id)
        async with steps.run("received") as step:
            step.summary = f"{parsed.filename} ({_size(parsed.size_bytes)}) via {parsed.source}"
            step.details = {
                "filename": parsed.filename,
                "size_bytes": parsed.size_bytes,
                "source": str(parsed.source),
                "sender_email": parsed.sender_email,
                "sha256": parsed.sha256,
                "storage_key": invoice.storage_key,
            }
        async with steps.run("read") as step:
            step.summary = _read_summary(parsed)
            step.details = {
                "kind": parsed.kind,
                "content_type": parsed.content_type,
                "pages": parsed.page_count,
                "attachment": parsed.attachment_name,
                "needs_transcription": parsed.needs_ocr,
                "page_images": len(parsed.page_images),
                "text_characters": len(parsed.text),
                "text_preview": parsed.text[:_TEXT_PREVIEW_CHARS],
            }
        return ProcessResult(invoice.id, already_received=False)

    # ── Phase 2: process ──────────────────────────────────────────────────────

    async def process(self, invoice_id: uuid.UUID) -> None:
        """Run the model calls, checks, and decision for a received invoice. Never raises."""
        invoice = await self._session.scalar(invoice_query().where(Invoice.id == invoice_id))
        if invoice is None or invoice.status not in IN_PROGRESS:
            return
        structlog.contextvars.bind_contextvars(invoice_id=str(invoice.id))
        # A restart can interrupt processing part-way: clear those steps and start again.
        await self._session.execute(
            delete(PipelineStep).where(
                PipelineStep.invoice_id == invoice_id,
                PipelineStep.step.not_in(RECEIVE_STEPS),
            )
        )
        invoice.status = InvoiceStatus.RECEIVED
        await self._session.commit()
        steps = StepRecorder(self._session, invoice_id)
        try:
            await self._process(invoice, steps)
        except AppError as exc:
            await self._fail(invoice_id, steps, exc.code, exc.message)
        except Exception:
            log.exception("pipeline_crashed")
            await self._fail(invoice_id, steps, "internal_error", "Processing failed unexpectedly.")

    async def _process(self, invoice: Invoice, steps: StepRecorder) -> None:
        parsed = self._parse(
            self._storage.read(invoice.storage_key), invoice.original_filename, invoice.content_type
        )
        text = await self._transcribe(invoice, parsed, steps)
        extraction = await self._extract(invoice, text, steps)

        async with steps.run("document_checks") as step:
            outcomes = run_document_checks(
                extraction.invoice,
                today=self._today(),
                tolerance=self._settings.amount_tolerance,
                # The current limit from the Settings page, else the configured default.
                approval_limit=await RulesService(self._session, self._settings).approval_limit(),
                base_currency=self._settings.base_currency,
                stale_after_days=self._settings.stale_invoice_days,
                company_name=self._settings.company_name,
            )
            passed = sum(outcome.passed for outcome in outcomes)
            step.summary = f"{passed} of {len(outcomes)} passed"
            step.details = {"checks": [_check_row(outcome) for outcome in outcomes]}
            if passed < len(outcomes):
                step.status = "warning"
        findings = [finding for outcome in outcomes for finding in outcome.findings]

        async with steps.run("master_data") as step:
            match = await MatchingService(
                self._session,
                fingerprint_key=self._settings.bank_fingerprint_key.get_secret_value().encode(),
                price_tolerance_pct=self._settings.po_price_tolerance_pct,
                tolerance_max_amount=self._settings.po_tolerance_max_amount,
                duplicate_window_days=self._settings.duplicate_window_days,
            ).match(extraction.invoice, invoice_id=invoice.id, sender_email=invoice.sender_email)
            self._apply_match(invoice, match)
            failed = [check for check in match.checks if check.passed is False]
            applicable = [check for check in match.checks if check.passed is not None]
            step.summary = f"{len(applicable) - len(failed)} of {len(applicable)} passed" + (
                f", {len(match.checks) - len(applicable)} not applicable"
                if len(applicable) < len(match.checks)
                else ""
            )
            step.details = {
                "vendor": match.vendor.name if match.vendor else None,
                "purchase_order": match.purchase_order.po_number if match.purchase_order else None,
                "checks": [
                    {"name": c.name, "label": c.label, "passed": c.passed, "detail": c.detail}
                    for c in match.checks
                ],
            }
            if failed:
                step.status = "warning"
        findings.extend(match.findings)

        judgment = await self._judge(text, steps)
        await self._code(invoice, match, steps)

        async with steps.run("decision") as step:
            outcome = self._decisions.decide_from(findings, judgment)
            deciding = _deciding_reasons(outcome)
            step.summary = str(outcome.action) + (
                f" because of {', '.join(deciding)}" if deciding else ": no concerns at all"
            )
            step.details = {
                "action": str(outcome.action),
                "band": str(outcome.band),
                "deciding_reasons": deciding,
                "all_reasons": [str(code) for code in outcome.reason_codes],
                "findings": [finding.model_dump(mode="json") for finding in outcome.findings],
                "rationale": outcome.rationale,
                "policy_version": POLICY_VERSION,
                "inputs_sha256": outcome.inputs_sha256,
            }

        async with steps.run("outcome") as step:
            task = await self._record_decision(invoice, outcome)
            if task is None:
                step.summary = "Approved automatically. No person needed."
            else:
                step.summary = "Sent to the review queue for a person to decide."
                step.details = {"review_task_id": str(task.id)}
            step.details["status"] = str(invoice.status)

    async def _transcribe(
        self, invoice: Invoice, parsed: ParsedDocument, steps: StepRecorder
    ) -> str:
        if not parsed.needs_ocr:
            await steps.skip("transcribe", "Not needed: the document has a text layer")
            return parsed.text
        async with steps.run("transcribe") as step:
            if self._transcription is None:
                raise AppError(
                    "This is a scan or photo, and reading images is not configured"
                    " (set VISION_MODEL, and GROQ_API_KEY or VISION_PROVIDER=ollama)."
                )
            await step.progress(
                f"Sending {len(parsed.page_images)} page image(s) to the vision model"
            )
            transcription = await self._transcription.transcribe(parsed.page_images)
            invoice.transcribed_text = transcription.text
            invoice.transcription_model = transcription.model
            invoice.transcription_cost_usd = transcription.cost_usd
            audit.record(
                self._session,
                action="invoice.transcribed",
                entity_type="invoice",
                entity_id=invoice.id,
                data={
                    "model": transcription.model,
                    "pages": transcription.pages,
                    "prompt_version": transcription.prompt_version,
                    "cost_usd": str(transcription.cost_usd),
                },
            )
            step.summary = (
                f"Read {transcription.pages} page(s) with {transcription.model}:"
                f" {len(transcription.text):,} characters"
            )
            step.details = {
                "model": transcription.model,
                "pages": transcription.pages,
                "prompt_version": transcription.prompt_version,
                "cost_usd": transcription.cost_usd,
                "text": transcription.text,
                "instructions": self._transcription.instructions,
            }
        return transcription.text

    async def _extract(self, invoice: Invoice, text: str, steps: StepRecorder) -> ExtractionResult:
        async with steps.run("extract") as step:
            if self._extraction is None:
                raise AppError("Extraction is not configured (GROQ_API_KEY is missing).")
            await step.progress("Asking the extraction model to read the fields")
            result = await self._extraction.extract(text)
            self._apply_extraction(invoice, result)
            fields = result.invoice.model_dump(mode="json")
            read = [name for name, value in fields.items() if value not in (None, [], "")]
            step.summary = (
                f"Read {len(read)} fields and {len(result.invoice.line_items)} line(s)"
                f" with {result.call.model} in {result.call.latency_ms / 1000:.1f} s"
            )
            step.details = {
                "provider": result.call.provider,
                "model": result.call.model,
                "prompt_version": result.prompt_version,
                "input_tokens": result.call.input_tokens,
                "output_tokens": result.call.output_tokens,
                "cost_usd": result.call.cost_usd,
                "latency_ms": result.call.latency_ms,
                "fields": fields,
                "bank_account_last4": (
                    bank_account_last4(result.invoice.bank_account)
                    if result.invoice.bank_account
                    else None
                ),
                "instructions": SYSTEM_PROMPT,
                "document_characters": len(text),
            }
        return result

    async def _code(self, invoice: Invoice, match: MatchResult, steps: StepRecorder) -> None:
        """Code each line to an account: the PO's, Jev's confident choice, or a fallback."""
        if not invoice.lines:
            await steps.skip(
                "coding", "No lines to code: the whole invoice uses the default account"
            )
            return
        async with steps.run("coding") as step:
            coding = await CodingService(
                self._session,
                self._decisions.jev_client,
                suspense_account=self._settings.export_suspense_account,
                min_confidence=self._settings.coding_min_confidence,
            ).code(
                invoice.lines,
                vendor=match.vendor,
                purchase_order=match.purchase_order,
                invoice_description=invoice.description,
            )
            names = dict(
                (row.code, row.name)
                for row in await self._session.execute(select(GLAccount.code, GLAccount.name))
            )
            by_source = Counter(line.source for line in coding.lines)
            step.summary = ", ".join(
                f"{count} {SOURCE_WORDS[source]}" for source, count in by_source.items()
            )
            step.details = {
                "model": coding.model,
                "jev_status": coding.jev_status,
                "question_set_version": CODING_QUESTION_SET,
                "latency_ms": coding.latency_ms,
                "cost_usd": coding.cost_usd,
                "min_confidence": self._settings.coding_min_confidence,
                "questions": coding.questions,
                "lines": [
                    {
                        "line_no": line.line_no,
                        "description": line.description,
                        "amount": line.amount,
                        "account": line.account,
                        "account_name": names.get(line.account, line.account),
                        "source": line.source,
                        "confidence": line.confidence,
                        "suggestion": line.suggestion,
                        "suggestion_name": names.get(line.suggestion) if line.suggestion else None,
                    }
                    for line in coding.lines
                ],
            }
            if by_source.get("suspense"):
                step.status = "warning"

    async def _judge(self, text: str, steps: StepRecorder) -> Judgment:
        async with steps.run("judgment") as step:
            wait = self._decisions.expected_wait_seconds()
            if wait > 0:
                await step.progress(
                    f"Waiting {wait:.0f} s for Jev's rate limit (free tier: one call per minute)"
                )
            elif self._decisions.jev_configured:
                await step.progress("Asking Jev the judgment questions")
            judgment = await self._decisions.judge(text)
            questions = {
                name: _question_text(question) for name, question in build_questions().items()
            }
            result = judgment.result
            details: dict[str, Any] = {
                "status": judgment.status,
                "question_set_version": QUESTION_SET_VERSION,
                "questions": questions,
                "answers": judgment.answers.model_dump(mode="json") if judgment.answers else None,
            }
            if result is not None:
                details.update(
                    model=result.model,
                    latency_ms=result.latency_ms,
                    waited_seconds=result.waited_seconds,
                    input_tokens=result.input_tokens,
                    cost_usd=result.cost_usd,
                )
                step.summary = (
                    f"Jev answered {len(questions)} questions in {result.latency_ms / 1000:.1f} s"
                    + (
                        f" (after waiting {result.waited_seconds:.0f} s for the rate limit)"
                        if result.waited_seconds
                        else ""
                    )
                )
            elif judgment.status == "not_configured":
                step.summary = "Jev is not connected, so a person will decide"
                step.status = "warning"
            else:
                step.summary = f"Jev did not answer ({judgment.error}), so a person will decide"
                details["error"] = judgment.error
                step.status = "warning"
            step.details = details
        return judgment

    async def _fail(
        self, invoice_id: uuid.UUID, steps: StepRecorder, code: str, message: str
    ) -> None:
        # Start from a clean transaction: whatever failed may have left pending changes.
        await self._session.rollback()
        invoice = await self._session.get(Invoice, invoice_id)
        if invoice is None:
            return
        invoice.status = InvoiceStatus.FAILED
        invoice.error_message = message
        audit.record(
            self._session,
            action="invoice.extraction_failed",
            entity_type="invoice",
            entity_id=invoice.id,
            data={"error": code, "message": message},
        )
        if await self._session.scalar(
            select(PipelineStep.id).where(
                PipelineStep.invoice_id == invoice_id, PipelineStep.step == "outcome"
            )
        ):
            await self._session.commit()  # the outcome step itself failed and is recorded
            return
        async with steps.run("outcome") as step:
            step.summary = f"Could not be processed: {message}"
            step.details = {"status": str(InvoiceStatus.FAILED), "error": code}
            step.status = "failed"

    # ── helpers ───────────────────────────────────────────────────────────────

    def _parse(self, data: bytes, filename: str, content_type: str | None) -> ParsedDocument:
        return parse_document(
            data,
            filename,
            content_type,
            max_pages=self._settings.vision_max_pages,
            max_image_px=self._settings.vision_max_image_px,
        )

    def _new_invoice(self, parsed: ParsedDocument, data: bytes) -> Invoice:
        extension = PurePath(parsed.filename).suffix.lower()[:10] or ".bin"
        storage_key = self._storage.save(
            data, sha256=hashlib.sha256(data).hexdigest(), extension=extension
        )
        return Invoice(
            id=uuid.uuid4(),
            status=InvoiceStatus.RECEIVED,
            source=parsed.source,
            sender_email=parsed.sender_email,
            original_filename=parsed.filename[:255],
            content_type=parsed.content_type,
            file_size_bytes=parsed.size_bytes,
            file_sha256=parsed.sha256,
            storage_key=storage_key,
            # Initialised so later assignment never triggers a lazy load (not allowed in async).
            lines=[],
            decisions=[],
        )

    def _apply_extraction(self, invoice: Invoice, result: ExtractionResult) -> None:
        fields = result.invoice
        invoice.vendor_name = fields.vendor_name
        invoice.invoice_number = fields.invoice_number
        invoice.invoice_number_normalized = (
            normalize_reference(fields.invoice_number) if fields.invoice_number else None
        )
        invoice.invoice_date = fields.invoice_date
        invoice.due_date = fields.due_date
        invoice.currency = fields.currency
        invoice.subtotal = fields.subtotal
        invoice.discount = fields.discount
        invoice.shipping = fields.shipping
        invoice.tax_total = fields.tax
        invoice.total = fields.total
        invoice.amount_paid = fields.amount_paid
        invoice.amount_due = fields.amount_due
        invoice.po_reference = fields.po_reference
        invoice.description = fields.description
        invoice.bill_to_name = fields.bill_to_name
        invoice.vendor_tax_id = fields.vendor_tax_id
        invoice.additional_info = {
            "description_printed": fields.description_printed,
            "payment_terms": fields.payment_terms_text,
            "late_fee_terms": fields.late_fee_terms,
            "remittance": fields.remittance_details,
            "notes": fields.notes,
            "other_details": [detail.model_dump() for detail in fields.other_details],
        }
        invoice.lines = [
            InvoiceLine(
                line_no=number,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                amount=line.amount,
            )
            for number, line in enumerate(fields.line_items, start=1)
        ]
        invoice.extraction_provider = result.call.provider
        invoice.extraction_model = result.call.model
        invoice.extraction_prompt_version = result.prompt_version
        invoice.extraction_cost_usd = result.call.cost_usd
        invoice.raw_extraction = result.raw
        invoice.status = InvoiceStatus.EXTRACTED
        audit.record(
            self._session,
            action="invoice.extracted",
            entity_type="invoice",
            entity_id=invoice.id,
            data={
                "model": result.call.model,
                "prompt_version": result.prompt_version,
                "cost_usd": str(result.call.cost_usd),
            },
        )

    def _apply_match(self, invoice: Invoice, match: MatchResult) -> None:
        invoice.vendor_id = match.vendor.id if match.vendor else None
        invoice.purchase_order_id = match.purchase_order.id if match.purchase_order else None
        invoice.bank_account_last4 = match.bank_account_last4
        invoice.bank_account_fingerprint = match.bank_account_fingerprint
        # Remember which PO line each invoice line billed, so later invoices against the
        # same PO count these quantities as already billed.
        for index, line in enumerate(invoice.lines):
            line.purchase_order_line_id = match.line_matches.get(index)

    async def _record_decision(
        self, invoice: Invoice, outcome: DecisionOutcome
    ) -> ReviewTask | None:
        if outcome.jev is not None:
            invoice.document_type = DocumentType(outcome.jev.document_type)
        decision = Decision(
            id=uuid.uuid4(),
            invoice_id=invoice.id,
            action=outcome.action,
            band=outcome.band,
            reason_codes=[str(code) for code in outcome.reason_codes],
            findings=[finding.model_dump(mode="json") for finding in outcome.findings],
            rationale=outcome.rationale,
            jev_status=outcome.jev_status,
            jev_answers=outcome.jev.model_dump(mode="json") if outcome.jev else None,
            jev_model=outcome.jev_model,
            jev_latency_ms=outcome.jev_latency_ms,
            question_set_version=outcome.question_set_version,
            policy_version=outcome.policy_version,
            inputs_sha256=outcome.inputs_sha256,
        )
        self._session.add(decision)
        audit.record(
            self._session,
            action="decision.made",
            entity_type="invoice",
            entity_id=invoice.id,
            data={
                "decision_id": str(decision.id),
                "action": outcome.action,
                "band": outcome.band,
                "reasons": decision.reason_codes,
            },
        )

        if outcome.band == ActionBand.AUTO:
            invoice.status = InvoiceStatus.APPROVED
            audit.record(
                self._session,
                action="invoice.auto_approved",
                entity_type="invoice",
                entity_id=invoice.id,
            )
            return None

        invoice.status = InvoiceStatus.PENDING_REVIEW
        task = ReviewTask(
            id=uuid.uuid4(),
            invoice_id=invoice.id,
            decision_id=decision.id,
            status=ReviewStatus.OPEN,
        )
        self._session.add(task)
        audit.record(
            self._session,
            action="review.opened",
            entity_type="review_task",
            entity_id=task.id,
            data={"invoice_id": str(invoice.id), "proposed_action": outcome.action},
        )
        return task


def _size(size_bytes: int) -> str:
    return f"{size_bytes / 1024:.0f} KB" if size_bytes >= 1024 else f"{size_bytes} bytes"


def _read_summary(parsed: ParsedDocument) -> str:
    if parsed.needs_ocr:
        what = "scanned PDF" if parsed.kind == "pdf" else "image"
        if parsed.attachment_name:
            what = f"email with {what} attachment"
        article = "An" if what[0] in "aeiou" else "A"
        images = len(parsed.page_images)
        return f"{article} {what} with no text layer: {images} page image(s) to transcribe"
    pages = f", {parsed.page_count} page(s)" if parsed.page_count else ""
    what = {"pdf": "PDF", "text": "Text file", "email": "Email"}.get(parsed.kind, parsed.kind)
    return f"{what}{pages}: {len(parsed.text):,} characters of text"


def _check_row(outcome: CheckOutcome) -> dict[str, Any]:
    return {
        "name": outcome.name,
        "label": outcome.label,
        "passed": outcome.passed,
        "findings": [finding.message for finding in outcome.findings],
    }


def _deciding_reasons(outcome: DecisionOutcome) -> list[str]:
    """The reasons in the highest-priority group: the ones that chose the action."""
    present = set(outcome.reason_codes)
    for _action, _band, triggers in ACTION_PRIORITY:
        if present & triggers:
            return [str(code) for code in outcome.reason_codes if code in triggers]
    return []


def _question_text(question: Any) -> str:
    instructions = getattr(question, "instructions", None)
    return str(instructions) if instructions else ""
