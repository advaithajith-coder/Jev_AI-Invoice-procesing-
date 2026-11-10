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
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import PurePath
from typing import Any, cast

import structlog
from sqlalchemy import Select, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jev_invoice.accounting.controls import check_accounting
from jev_invoice.coding.service import QUESTION_SET_VERSION as CODING_QUESTION_SET
from jev_invoice.coding.service import CodingService
from jev_invoice.config import Settings
from jev_invoice.core import audit
from jev_invoice.core.errors import AppError, IncompleteDocumentError
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
from jev_invoice.decision.routing import route_exceptions
from jev_invoice.decision.service import DecisionService, Judgment
from jev_invoice.domain.checks import CheckOutcome, run_document_checks
from jev_invoice.domain.decision import DecisionOutcome, JevAnswers, JevStatus
from jev_invoice.domain.enums import (
    ActionBand,
    DecisionAction,
    DocumentType,
    InvoiceStatus,
    ReasonCode,
    ReviewStatus,
)
from jev_invoice.domain.invoice import CheckFinding, ExtractedInvoice
from jev_invoice.domain.vendors import bank_account_last4
from jev_invoice.evaluation.telemetry import current_calls
from jev_invoice.extraction.prompts import SYSTEM_PROMPT
from jev_invoice.extraction.service import ExtractionResult, ExtractionService
from jev_invoice.extraction.vision import TranscriptionService
from jev_invoice.fx.service import ExchangeRates, Rate, RateSource
from jev_invoice.ingest.parsing import ParsedDocument, join_pages, parse_document
from jev_invoice.ingest.storage import LocalFileStorage
from jev_invoice.masterdata.rules import RulesService
from jev_invoice.matching.service import MatchingService, MatchResult, normalize_reference
from jev_invoice.pipeline.approval import approval_findings
from jev_invoice.pipeline.exceptions import sync_exceptions
from jev_invoice.pipeline.steps import StepRecorder
from jev_invoice.tax.massachusetts import check_tax

log = structlog.get_logger(__name__)

_TEXT_PREVIEW_CHARS = 4000
SOURCE_WORDS = {
    "validated_correction": "from a validated correction",
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
        rates: RateSource | None = None,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage
        self._extraction = extraction
        self._decisions = decisions
        self._transcription = transcription
        self._rates = rates
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
                "text_pages": [i for i, text in enumerate(parsed.page_texts, 1) if text],
                "vision_pages": [page.page_number for page in parsed.page_images],
                "omitted_pages": list(parsed.omitted_pages),
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
        calls: list[dict[str, Any]] = []
        token = current_calls.set(calls)
        started = time.monotonic()
        try:
            await self._process(invoice, steps)
        except IncompleteDocumentError as exc:
            await self._hold_incomplete(invoice, steps, exc)
        except AppError as exc:
            await self._fail(invoice_id, steps, exc.code, exc.message)
        except Exception:
            log.exception("pipeline_crashed")
            await self._fail(invoice_id, steps, "internal_error", "Processing failed unexpectedly.")
        finally:
            current_calls.reset(token)
            stored = await self._session.get(Invoice, invoice_id)
            if stored is not None:
                info = dict(stored.additional_info or {})
                history = list(info.get("processing_measurements", []))
                history.append(
                    {
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "wall_time_ms": round((time.monotonic() - started) * 1000, 1),
                        "calls": calls,
                        "note": "Job execution time includes waits and retries. Upload queue time "
                        "is excluded. SDK-internal retry charges may be unreported.",
                    }
                )
                stored.additional_info = {**info, "processing_measurements": history}
                await self._session.commit()

    async def _hold_incomplete(
        self, invoice: Invoice, steps: StepRecorder, error: IncompleteDocumentError
    ) -> None:
        coverage = dict((invoice.additional_info or {}).get("document_coverage", {}))
        coverage.update(error.details)
        coverage.update(status="incomplete", reason=error.message)
        invoice.additional_info = {**(invoice.additional_info or {}), "document_coverage": coverage}
        outcome = DecisionOutcome(
            action=DecisionAction.HOLD_REVIEW,
            band=ActionBand.HUMAN,
            reason_codes=[ReasonCode.INCOMPLETE_DOCUMENT],
            rationale=error.message,
            findings=[CheckFinding(code=ReasonCode.INCOMPLETE_DOCUMENT, message=error.message)],
            jev_status="not_run",
            question_set_version="document-coverage-v1",
            policy_version=POLICY_VERSION,
            inputs_sha256=hashlib.sha256(
                f"{invoice.file_sha256}:{error.message}".encode()
            ).hexdigest(),
        )
        outcome.exception_routing = await route_exceptions(
            outcome, None, self._settings.coding_min_confidence
        )
        async with steps.run("outcome") as step:
            task = await self._record_decision(invoice, outcome)
            step.summary = error.message
            step.details = {"coverage": coverage, "review_task_id": str(task.id) if task else None}

    async def _process(self, invoice: Invoice, steps: StepRecorder) -> None:
        parsed = self._parse(
            self._storage.read(invoice.storage_key), invoice.original_filename, invoice.content_type
        )
        text = await self._transcribe(invoice, parsed, steps)
        extraction = await self._extract(invoice, text, steps)
        rate = await self._exchange_rate(invoice)

        findings = await self._check_document(extraction.invoice, rate, steps)
        match = await self._check_records(invoice, extraction.invoice, rate, steps)
        findings.extend(match.findings)

        judgment = await self._judge(text, steps)
        await self._code(invoice, match, steps, text)
        findings.extend(_finance_findings(invoice))
        findings.extend(
            approval_findings(invoice, suspense_account=self._settings.export_suspense_account)
        )
        findings.extend(await check_accounting(self._session, invoice))
        if self._settings.massachusetts_tax_checks_enabled:
            findings.extend(
                await check_tax(invoice, self._decisions.jev_client, session=self._session)
            )
        outcome = await self._decide(findings, judgment, steps)
        routing = await route_exceptions(
            outcome, self._decisions.jev_client, self._settings.coding_min_confidence
        )
        outcome = outcome.model_copy(update={"exception_routing": routing})

        async with steps.run("outcome") as step:
            task = await self._record_decision(invoice, outcome)
            if task is None:
                step.summary = "Approved automatically. No person needed."
            else:
                step.summary = "Sent to the review queue for a person to decide."
                step.details = {"review_task_id": str(task.id)}
            step.details["status"] = str(invoice.status)

    # ── Re-check: after a delivery, with no model calls ───────────────────────

    async def recheck(self, invoice_id: uuid.UUID, *, actor: str, why: str) -> bool:
        """Check a waiting invoice again against today's records, such as a new delivery.

        The fields already read and Jev's answers are reused, so no model is called. The
        document checks, vendor and PO checks, decision and outcome are recorded again, and
        a new decision is added (earlier ones are kept). With no reasons left the invoice is
        approved and its review closed; otherwise the review stays open with the new
        reasons. Returns True when the invoice was approved.
        """
        invoice = await self._session.scalar(invoice_query().where(Invoice.id == invoice_id))
        if (
            invoice is None
            or invoice.status != InvoiceStatus.PENDING_REVIEW
            or invoice.raw_extraction is None
            or not invoice.decisions
            or invoice.hold_kind  # a person put it on hold: leave it to them
        ):
            return False
        task = await self._session.scalar(
            select(ReviewTask).where(
                ReviewTask.invoice_id == invoice.id, ReviewTask.status == ReviewStatus.OPEN
            )
        )
        if task is None:
            return False
        previous = invoice.decisions[-1]
        extracted = ExtractedInvoice.model_validate(invoice.raw_extraction)
        judgment = Judgment(
            JevAnswers.model_validate(previous.jev_answers) if previous.jev_answers else None,
            cast(JevStatus, previous.jev_status),
        )
        await self._session.execute(
            delete(PipelineStep).where(
                PipelineStep.invoice_id == invoice.id,
                PipelineStep.step.in_(("document_checks", "master_data", "decision", "outcome")),
            )
        )
        await self._session.commit()
        steps = StepRecorder(self._session, invoice.id)
        rate = await self._exchange_rate(invoice)
        findings = await self._check_document(extracted, rate, steps)
        match = await self._check_records(
            invoice,
            extracted,
            rate,
            steps,
            stored_bank=(invoice.bank_account_last4, invoice.bank_account_fingerprint),
        )
        findings.extend(match.findings)
        findings.extend(_finance_findings(invoice))
        findings.extend(
            approval_findings(invoice, suspense_account=self._settings.export_suspense_account)
        )
        findings.extend(await check_accounting(self._session, invoice))
        if self._settings.massachusetts_tax_checks_enabled:
            findings.extend(await check_tax(invoice, session=self._session))
        outcome = await self._decide(findings, judgment, steps)
        # Tax rechecks reuse saved treatments and never call a model.
        outcome = outcome.model_copy(
            update={
                "jev_model": previous.jev_model,
                "jev_latency_ms": previous.jev_latency_ms,
                "exception_routing": [
                    r for r in previous.exception_routing or [] if r["code"] in outcome.reason_codes
                ],
            }
        )

        existing_routes = {r["code"]: r for r in outcome.exception_routing}
        defaults = await route_exceptions(outcome, None, self._settings.coding_min_confidence)
        refreshed_routes = []
        for fallback in defaults:
            route = existing_routes.get(fallback["code"], fallback)
            current_evidence = [f.message for f in outcome.findings if str(f.code) == route["code"]]
            refreshed_routes.append(
                {
                    **route,
                    "evidence": current_evidence,
                    "reason": f"{' '.join(current_evidence)} Responsible team: {route['owner']}.",
                    "evidence_rechecked": True,
                }
            )
        outcome = outcome.model_copy(update={"exception_routing": refreshed_routes})
        async with steps.run("outcome") as step:
            decision = self._new_decision(invoice, outcome)
            await sync_exceptions(self._session, invoice, decision, actor=actor)
            audit.record(
                self._session,
                action="invoice.rechecked",
                entity_type="invoice",
                entity_id=invoice.id,
                actor=actor,
                data={"why": why, "action": outcome.action, "reasons": decision.reason_codes},
            )
            if outcome.band == ActionBand.AUTO:
                invoice.status = InvoiceStatus.APPROVED
                task.status = ReviewStatus.APPROVED
                task.resolved_by = actor
                task.resolved_at = datetime.now(UTC)
                task.resolution_note = f"Checked again: {why}. Every check now passes."
                audit.record(
                    self._session,
                    action="invoice.auto_approved",
                    entity_type="invoice",
                    entity_id=invoice.id,
                )
                step.summary = f"Approved after checking again: {why}."
            else:
                task.decision_id = decision.id
                step.summary = "Still in the review queue after checking again."
                step.details = {"review_task_id": str(task.id)}
            step.details["status"] = str(invoice.status)
        return outcome.band == ActionBand.AUTO

    # ── Shared steps ──────────────────────────────────────────────────────────

    async def _check_document(
        self, extracted: ExtractedInvoice, rate: Rate | None, steps: StepRecorder
    ) -> list[CheckFinding]:
        async with steps.run("document_checks") as step:
            outcomes = run_document_checks(
                extracted,
                today=self._today(),
                tolerance=self._settings.amount_tolerance,
                # The current limit from the Settings page, else the configured default.
                approval_limit=await RulesService(self._session, self._settings).approval_limit(),
                base_currency=self._settings.base_currency,
                stale_after_days=self._settings.stale_invoice_days,
                company_name=self._settings.company_name,
                fx_rate=rate.rate if rate else None,
                fx_note=_rate_note(rate) if rate else "",
            )
            passed = sum(outcome.passed for outcome in outcomes)
            step.summary = f"{passed} of {len(outcomes)} passed"
            step.details = {"checks": [_check_row(outcome) for outcome in outcomes]}
            if passed < len(outcomes):
                step.status = "warning"
        return [finding for outcome in outcomes for finding in outcome.findings]

    async def _check_records(
        self,
        invoice: Invoice,
        extracted: ExtractedInvoice,
        rate: Rate | None,
        steps: StepRecorder,
        *,
        stored_bank: tuple[str | None, str | None] | None = None,
    ) -> MatchResult:
        async with steps.run("master_data") as step:
            match = await MatchingService(
                self._session,
                fingerprint_key=self._settings.bank_fingerprint_key.get_secret_value().encode(),
                price_tolerance_pct=self._settings.po_price_tolerance_pct,
                # The cap is in the company's currency; a PO in the invoice's currency
                # compares against the same amount converted.
                tolerance_max_amount=(
                    (self._settings.po_tolerance_max_amount / rate.rate).quantize(Decimal("0.01"))
                    if rate
                    else self._settings.po_tolerance_max_amount
                ),
                duplicate_window_days=self._settings.duplicate_window_days,
            ).match(
                extracted,
                invoice_id=invoice.id,
                sender_email=invoice.sender_email,
                stored_bank=stored_bank,
            )
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
        return match

    async def _decide(
        self, findings: list[CheckFinding], judgment: Judgment, steps: StepRecorder
    ) -> DecisionOutcome:
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
        return outcome

    async def _exchange_rate(self, invoice: Invoice) -> Rate | None:
        """For an invoice in another currency: the rate to the company's, kept on the invoice.

        The rate is the one for the invoice date (today's when the date is missing or in the
        future). None for an invoice in the company's currency, or when no rate is found.
        """
        base = self._settings.base_currency
        if not invoice.currency or invoice.currency == base:
            return None
        today = self._today()
        on = (
            invoice.invoice_date
            if invoice.invoice_date and invoice.invoice_date <= today
            else today
        )
        rate = await ExchangeRates(self._session, base, self._rates).rate(invoice.currency, on)
        if rate is not None:
            invoice.fx_rate = rate.rate
            invoice.fx_rate_date = rate.rate_date
            invoice.fx_rate_source = rate.source
        return rate

    async def _transcribe(
        self, invoice: Invoice, parsed: ParsedDocument, steps: StepRecorder
    ) -> str:
        coverage: dict[str, Any] = {
            "status": "complete",
            "total_pages": parsed.page_count,
            "text_pages": [i for i, text in enumerate(parsed.page_texts, 1) if text],
            "vision_pages": [],
            "omitted_pages": list(parsed.omitted_pages),
        }
        invoice.additional_info = {**(invoice.additional_info or {}), "document_coverage": coverage}
        if parsed.omitted_pages:
            raise IncompleteDocumentError(
                "The document exceeds the configured vision page limit. Pages "
                + ", ".join(map(str, parsed.omitted_pages))
                + " were not read. Increase the limit and reprocess the complete document.",
                details=coverage,
            )
        if not parsed.needs_ocr:
            coverage["processed_pages"] = len(coverage["text_pages"])
            await steps.skip("transcribe", "Not needed: the document has a text layer")
            return parsed.text
        async with steps.run("transcribe") as step:
            if self._transcription is None:
                raise AppError(
                    "This is a scan or photo, and reading images is not configured"
                    " (set VISION_MODEL to a local Ollama model, e.g. glm-ocr:q8_0)."
                )
            await step.progress(
                f"Sending {len(parsed.page_images)} page image(s) to the vision model"
            )
            transcription = await self._transcription.transcribe(parsed.page_images)
            pages = list(parsed.page_texts)
            for number, text in transcription.page_texts:
                pages[number - 1] = text
            combined = join_pages(tuple(pages)) if pages else transcription.text
            coverage.update(
                vision_pages=[number for number, _ in transcription.page_texts],
                processed_pages=len(coverage["text_pages"]) + len(transcription.page_texts),
                unreadable_pages=list(transcription.unreadable_pages),
                truncated_pages=list(transcription.truncated_pages),
            )
            invoice.additional_info = {
                **(invoice.additional_info or {}),
                "document_coverage": coverage,
            }
            invoice.transcribed_text = combined
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
                "text": combined,
                "coverage": coverage,
                "instructions": self._transcription.instructions,
            }
            if transcription.unreadable_pages or transcription.truncated_pages:
                affected = sorted(
                    set(transcription.unreadable_pages + transcription.truncated_pages)
                )
                raise IncompleteDocumentError(
                    "Reading is incomplete on pages "
                    + ", ".join(map(str, affected))
                    + ". Supply readable complete pages before approval.",
                    details=coverage,
                )
        return combined

    async def _extract(self, invoice: Invoice, text: str, steps: StepRecorder) -> ExtractionResult:
        if self._extraction is not None:
            # Between steps: waiting on the model's rate limit is not processing time.
            await self._extraction.wait_for_capacity()
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

    async def _code(
        self, invoice: Invoice, match: MatchResult, steps: StepRecorder, text: str = ""
    ) -> None:
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
                document_text=text,
                buyer=invoice.bill_to_name,
                currency=invoice.currency,
                invoice_date=invoice.invoice_date,
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
                "classification": coding.classification,
                "finance_lines": [line.finance_classification for line in invoice.lines],
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
            **(invoice.additional_info or {}),
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

    def _new_decision(self, invoice: Invoice, outcome: DecisionOutcome) -> Decision:
        """Add the decision (decisions are append-only) and record it in the history."""
        if outcome.jev is not None:
            invoice.document_type = DocumentType(outcome.jev.document_type)
        decision = Decision(
            id=uuid.uuid4(),
            invoice_id=invoice.id,
            action=outcome.action,
            band=outcome.band,
            exception_routing=outcome.exception_routing,
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
                "exception_routing": decision.exception_routing,
            },
        )
        return decision

    async def _record_decision(
        self, invoice: Invoice, outcome: DecisionOutcome
    ) -> ReviewTask | None:
        decision = self._new_decision(invoice, outcome)
        await sync_exceptions(self._session, invoice, decision)

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


def _rate_note(rate: Rate) -> str:
    published = rate.rate_date.strftime("%d %b %Y").lstrip("0")
    return f"1 {rate.currency} = {rate.rate.normalize():f} {rate.base} ({rate.source}, {published})"


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


def _finance_findings(invoice: Invoice) -> list[CheckFinding]:
    findings = []
    for line in invoice.lines:
        finance = line.finance_classification or {}
        fields = finance.get("fields", {})
        reasons = [r for r in finance.get("review_reasons", []) if not r.startswith("Missing ")]
        for requirement, names in (
            ("service_dates_required", ("service_start", "service_end")),
            ("asset_details_required", ("asset_details",)),
        ):
            if fields.get(requirement, {}).get("value") is True:
                reasons.extend(
                    f"Missing {name.replace('_', ' ')}"
                    for name in names
                    if not fields.get(name, {}).get("value")
                )
        for reason in reasons:
            if reason.startswith("Missing ") or "conflict" in reason:
                findings.append(
                    CheckFinding(
                        code=ReasonCode.FINANCE_CLASSIFICATION_REVIEW,
                        message=f"Line {line.line_no}: {reason}",
                    )
                )
    return findings
