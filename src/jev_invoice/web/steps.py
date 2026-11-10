"""What each processing step produced for one invoice: one line per step.

The invoice page shows these lines, each opening to the step's full output. How the steps
work (the models, prompts, questions and rules) is described once, on the Workflow page.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.db.models import Decision, Invoice, PipelineStep, ReviewTask
from jev_invoice.domain.enums import InvoiceStatus
from jev_invoice.pipeline.steps import STEPS
from jev_invoice.web import labels

IN_PROGRESS = (InvoiceStatus.RECEIVED, InvoiceStatus.EXTRACTED)
STATUS_TONES = {
    "done": "ok",
    "warning": "human",
    "failed": "block",
    "running": "confirm",
    "pending": "muted",
    "skipped": "muted",
}
SOURCE_WORDS = {
    "validated_correction": "from a validated correction",
    "learned_correction": "learned from earlier reviewer corrections",
    "po": "from the PO",
    "ai": "chosen by Jev",
    "vendor": "vendor default",
    "suspense": "needs coding",
    "person": "set by a person",
}


@dataclass
class Step:
    key: str
    number: int
    title: str
    status: str  # pending | running | done | warning | failed | skipped
    line: str
    tone: str
    row: PipelineStep | None = None

    @property
    def details(self) -> dict[str, Any]:
        return self.row.details if self.row else {}

    @property
    def ms(self) -> float | None:
        if self.row is None or self.status in ("running", "skipped"):
            return None
        return float(self.row.duration_ms or 0)


@dataclass
class Steps:
    items: list[Step]
    finished: bool
    total_ms: float
    current: Step | None


async def load_steps(
    session: AsyncSession,
    invoice: Invoice,
    *,
    decision: Decision | None,
    open_task: ReviewTask | None,
    jev_rows: list[dict[str, Any]],
    account_names: dict[str, str],
) -> Steps:
    rows = {
        row.step: row
        for row in await session.scalars(
            select(PipelineStep).where(PipelineStep.invoice_id == invoice.id)
        )
    }
    outcome = rows.get("outcome")
    finished = invoice.status not in IN_PROGRESS or (
        outcome is not None and outcome.status != "running"
    )
    items = []
    for key, (number, title) in STEPS.items():
        row = rows.get(key)
        status = row.status if row else ("pending" if not finished else "skipped")
        line, tone = _line(key, status, row, invoice, decision, open_task, jev_rows, account_names)
        items.append(Step(key, number, title, status, line, tone, row))
    return Steps(
        items=items,
        finished=finished,
        total_ms=sum(float(row.duration_ms or 0) for row in rows.values()),
        current=next((step for step in items if step.status == "running"), None),
    )


def _line(
    key: str,
    status: str,
    row: PipelineStep | None,
    invoice: Invoice,
    decision: Decision | None,
    open_task: ReviewTask | None,
    jev_rows: list[dict[str, Any]],
    account_names: dict[str, str],
) -> tuple[str, str]:
    """The step's output in a few words, and its tone."""
    tone = STATUS_TONES.get(status, "muted")
    if status == "pending":
        return "Waiting for the steps before it", tone
    if status == "running":
        return (row.summary if row and row.summary else "In progress"), tone
    if status == "skipped":
        return ("Not needed: the file has text" if key == "transcribe" else "Did not run"), tone
    if status == "failed":
        if key == "outcome":
            return _outcome(invoice, open_task)
        return (row.summary if row and row.summary else "Failed"), tone
    details = row.details if row else {}

    if key == "received":
        how = (
            f"emailed by {invoice.sender_email}"
            if invoice.source == "email" and invoice.sender_email
            else "uploaded"
        )
        return f"{invoice.original_filename}, {how}", tone
    if key == "read":
        kind = {"pdf": "PDF", "email": "Email", "image": "Image", "text": "Text file"}.get(
            str(details.get("kind")), str(details.get("kind") or "File")
        )
        if details.get("needs_transcription"):
            return f"{kind} with no text: {details.get('page_images', 0)} page image(s)", tone
        return f"{kind} with text, {details.get('text_characters', 0):,} characters", tone
    if key == "transcribe":
        text = invoice.transcribed_text or ""
        return f"{details.get('pages', 1)} page(s) read, {len(text):,} characters", tone
    if key == "extract":
        parts = [
            invoice.invoice_number or "no invoice number",
            _date(invoice.invoice_date) or "no date",
            invoice.po_reference or "no PO",
            f"{len(invoice.lines)} line{'s' if len(invoice.lines) != 1 else ''}",
        ]
        if invoice.total is not None:
            parts.append(_money(invoice.total, invoice.currency))
        return " · ".join(parts), tone
    if key in ("document_checks", "master_data"):
        checks = details.get("checks") or []
        applicable = [check for check in checks if check.get("passed") is not None]
        failed = [check["label"] for check in applicable if check.get("passed") is False]
        if not failed:
            return f"All {len(applicable)} passed", "ok"
        return f"{len(failed)} of {len(applicable)} failed: {'; '.join(failed)}", "human"
    if key == "judgment":
        if not jev_rows:
            return (row.summary if row and row.summary else "No answer"), "human"
        flagged = [r["label"] for r in jev_rows if r["verdict"][0] == "flagged"]
        unsure = [r["label"] for r in jev_rows if r["verdict"][0] == "unsure"]
        if flagged:
            return f"Flagged: {', '.join(flagged)}", "block"
        if unsure:
            return f"Unsure about: {', '.join(unsure)}", "human"
        kind = jev_rows[0]["value"].split(" (")[0]
        return f"{kind}, asks for payment, no risk signals", "ok"
    if key == "coding":
        coded = {(line.gl_account_code, line.gl_source) for line in invoice.lines}
        if not invoice.lines:
            return "No lines to code", "muted"
        if len(coded) == 1:
            code, source = next(iter(coded))
            if not code:
                return "Not coded", "human"
            how = SOURCE_WORDS.get(source or "", source or "")
            name = account_names.get(code, "")
            return f"{code} {name} ({how})", "human" if source == "suspense" else "ok"
        codes = sorted({code for code, _ in coded if code})
        return f"{len(codes)} accounts: {', '.join(codes)}", tone
    if key == "decision":
        if decision is None:
            return "No decision", "muted"
        label, action_tone = labels.ACTIONS[decision.action]
        reasons = [labels.reason(code).title for code in decision.reason_codes]
        return (
            f"{label}: {', '.join(reasons)}" if reasons else f"{label}, no reasons"
        ), action_tone
    if key == "outcome":
        return _outcome(invoice, open_task)
    return (row.summary if row and row.summary else ""), tone


def _outcome(invoice: Invoice, open_task: ReviewTask | None) -> tuple[str, str]:
    status = invoice.status
    if status == InvoiceStatus.PENDING_REVIEW:
        who = (
            f", assigned to {open_task.assigned_to}" if open_task and open_task.assigned_to else ""
        )
        return f"In the review queue{who}", "human"
    if status == InvoiceStatus.APPROVED:
        return "Approved, ready to export", "ok"
    if status == InvoiceStatus.EXPORTED:
        return "Exported to accounting", "ok"
    if status == InvoiceStatus.REJECTED:
        return "Rejected", "block"
    if status == InvoiceStatus.FAILED:
        return "Could not be read. Enter this invoice by hand, or upload a clearer copy", "block"
    return labels.STATUSES.get(status, (str(status), "muted"))


def _money(value: Decimal, currency: str | None) -> str:
    return f"{currency + ' ' if currency else ''}{value:,.2f}"


def _date(value: Any) -> str:
    return value.strftime("%d %b %Y").lstrip("0") if value else ""
