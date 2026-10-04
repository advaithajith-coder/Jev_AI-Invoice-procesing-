"""The journey screen: every step one invoice went through, live while it happens.

/invoices/{id}/journey       full page
/invoices/{id}/journey/live  just the journey, polled every second while processing
"""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from jev_invoice.api.dependencies import SessionDep
from jev_invoice.core.errors import NotFoundError
from jev_invoice.db.models import AuditEvent, Invoice, PipelineStep, ReviewTask
from jev_invoice.domain.enums import InvoiceStatus
from jev_invoice.pipeline.service import invoice_query
from jev_invoice.pipeline.steps import STEPS
from jev_invoice.web.routes import NOTICES, _jev_rows, templates

router = APIRouter(include_in_schema=False)

AFTER_PIPELINE = {"review.approved", "review.rejected", "invoice.exported"}
IN_PROGRESS = (InvoiceStatus.RECEIVED, InvoiceStatus.EXTRACTED)
COST_STEPS = ("transcribe", "extract", "judgment", "coding")

# Who handles each step: (kind, label). The kind also colours the time bar.
ACTORS: dict[str, tuple[str, str]] = {
    "received": ("code", "Code"),
    "read": ("code", "Code"),
    "transcribe": ("vision", "Vision model"),
    "extract": ("llm", "Language model"),
    "document_checks": ("rules", "Rules in code"),
    "master_data": ("rules", "Rules in code"),
    "judgment": ("jev", "TypeSafe Jev"),
    "coding": ("jev", "TypeSafe Jev"),
    "decision": ("policy", "Decision policy"),
    "outcome": ("workflow", "Workflow"),
}


@dataclass
class StepView:
    key: str
    number: int
    title: str
    status: str  # pending | running | done | warning | failed | skipped
    row: PipelineStep | None = None

    @property
    def details(self) -> dict[str, Any]:
        return self.row.details if self.row else {}

    @property
    def actor(self) -> str:
        return ACTORS.get(self.key, ("code", "Code"))[1]

    @property
    def model(self) -> str | None:
        details = self.details
        model = details.get("model")
        if self.key == "decision" and details.get("policy_version"):
            return f"policy {details['policy_version']}"
        return str(model) if model else None


@dataclass
class TimeSegment:
    label: str
    kind: str
    ms: float
    share: float  # percent of the total


@dataclass
class Journey:
    invoice: Invoice
    steps: list[StepView]
    finished: bool
    total_ms: float
    cost_usd: Decimal
    current: StepView | None
    after: list[AuditEvent] = field(default_factory=list)
    open_task: ReviewTask | None = None
    jev_rows: list[dict[str, Any]] = field(default_factory=list)
    time: list[TimeSegment] = field(default_factory=list)


def time_segments(steps: list[StepView]) -> list[TimeSegment]:
    """Where the time went, step by step; Jev's rate-limit wait is its own segment."""
    parts: list[tuple[str, str, float]] = []
    for step in steps:
        ms = float(step.row.duration_ms or 0) if step.row else 0.0
        if ms <= 0:
            continue
        kind = ACTORS.get(step.key, ("code", ""))[0]
        waited = _decimal(step.details.get("waited_seconds")) * 1000
        wait_ms = min(float(waited), ms) if step.key == "judgment" else 0.0
        if wait_ms > 0:
            parts.append(("Waiting for Jev's rate limit", "wait", wait_ms))
        if ms - wait_ms > 0:
            parts.append((step.title, kind, ms - wait_ms))
    total = sum(ms for _, _, ms in parts)
    if total <= 0:
        return []
    return [TimeSegment(label, kind, ms, ms / total * 100) for label, kind, ms in parts]


async def load_journey(session: SessionDep, invoice_id: uuid.UUID) -> Journey:
    session.expire_all()
    invoice = await session.scalar(invoice_query().where(Invoice.id == invoice_id))
    if invoice is None:
        raise NotFoundError("Invoice not found", details={"invoice_id": str(invoice_id)})

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
    steps = []
    for key, (number, title) in STEPS.items():
        row = rows.get(key)
        status = row.status if row else ("pending" if not finished else "skipped")
        steps.append(StepView(key, number, title, status, row))

    tasks = list(
        await session.scalars(select(ReviewTask).where(ReviewTask.invoice_id == invoice.id))
    )
    entity_ids = [str(invoice.id), *(str(task.id) for task in tasks)]
    after = list(
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_id.in_(entity_ids), AuditEvent.action.in_(AFTER_PIPELINE))
            .order_by(AuditEvent.id)
        )
    )
    judgment = rows.get("judgment")
    return Journey(
        invoice=invoice,
        steps=steps,
        finished=finished,
        total_ms=sum(row.duration_ms or 0 for row in rows.values()),
        cost_usd=sum(
            (_decimal(rows[key].details.get("cost_usd")) for key in COST_STEPS if key in rows),
            Decimal(0),
        ),
        current=next((step for step in steps if step.status == "running"), None),
        after=after,
        open_task=next((task for task in tasks if task.status == "open"), None),
        jev_rows=_jev_rows(judgment.details.get("answers")) if judgment else [],
        time=time_segments(steps) if finished else [],
    )


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value is not None else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


@router.get("/invoices/{invoice_id}/journey", response_class=HTMLResponse)
async def journey_page(
    request: Request, invoice_id: uuid.UUID, session: SessionDep
) -> HTMLResponse:
    journey = await load_journey(session, invoice_id)
    return templates.TemplateResponse(
        request,
        "journey.html",
        {
            "j": journey,
            "nav": "inbox",
            "notice": NOTICES.get(request.query_params.get("notice", "")),
        },
    )


@router.get("/invoices/{invoice_id}/journey/live", response_class=HTMLResponse)
async def journey_live(
    request: Request, invoice_id: uuid.UUID, session: SessionDep
) -> HTMLResponse:
    journey = await load_journey(session, invoice_id)
    return templates.TemplateResponse(request, "_journey.html", {"j": journey})
