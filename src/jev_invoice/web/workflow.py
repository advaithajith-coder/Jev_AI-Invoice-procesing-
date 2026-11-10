"""The workflow screen: the whole route on one live map.

/workflow       the page
/workflow/live  the numbers behind it (JSON), polled every few seconds

Everything shown comes from the database: average time per desk, how invoices split at
each fork, which invoices are on the route right now, and the latest outcomes. The page
animates sample slips in those proportions and marks real invoices at their desks.
"""

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select

from jev_invoice.api.dependencies import SessionDep
from jev_invoice.db.models import Invoice, PipelineStep
from jev_invoice.domain.enums import DecisionAction, InvoiceStatus
from jev_invoice.masterdata.rules import RulesService
from jev_invoice.pipeline.service import IN_PROGRESS, invoice_query
from jev_invoice.pipeline.steps import STEPS
from jev_invoice.web import labels
from jev_invoice.web.routes import ago, templates
from jev_invoice.web.rules import rules_context

router = APIRouter(include_in_schema=False)

SAMPLE = 500  # the most recent invoices that set the proportions
DEFAULT_SPLIT = {"scan": 0.3, "failed": 0.05, "auto": 0.35, "review_approved": 0.7}


async def workflow_data(session: SessionDep) -> dict[str, Any]:
    session.expire_all()
    invoices = list(
        await session.scalars(invoice_query().order_by(Invoice.received_at.desc()).limit(SAMPLE))
    )
    ids = [invoice.id for invoice in invoices]
    avg_ms = {
        step: round(float(ms), 1)
        for step, ms in await session.execute(
            select(PipelineStep.step, func.avg(PipelineStep.duration_ms))
            .where(PipelineStep.invoice_id.in_(ids), PipelineStep.status.in_(("done", "warning")))
            .group_by(PipelineStep.step)
        )
        if ms is not None
    }
    transcribe = Counter(
        dict(
            (status, count)
            for status, count in await session.execute(
                select(PipelineStep.status, func.count())
                .where(PipelineStep.invoice_id.in_(ids), PipelineStep.step == "transcribe")
                .group_by(PipelineStep.status)
            )
        )
    )
    in_flight = [invoice for invoice in invoices if invoice.status in IN_PROGRESS]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "sample": len(invoices),
        "avg_ms": avg_ms,
        "speed": await _speed(session, invoices),
        "split": _split(invoices, transcribe),
        "counts": _counts(invoices),
        "in_flight": [await _position(session, invoice) for invoice in in_flight],
        "recent": [_outcome(invoice) for invoice in invoices if invoice.status not in IN_PROGRESS][
            :6
        ],
    }


async def _speed(session: SessionDep, invoices: list[Invoice]) -> dict[str, Any] | None:
    """The fastest and typical time from arrival to a decision, in processing time.

    Only invoices that reached a decision count. Time is the sum of their steps, so
    waiting in a queue (or for a rate limit) is left out, as on the desks above.
    """
    decided = {
        invoice.id: invoice
        for invoice in invoices
        if invoice.status not in IN_PROGRESS
        and invoice.status != InvoiceStatus.FAILED
        and invoice.decisions
    }
    if not decided:
        return None
    totals = sorted(
        (float(ms), invoice_id)
        for invoice_id, ms in await session.execute(
            select(PipelineStep.invoice_id, func.sum(PipelineStep.duration_ms))
            .where(PipelineStep.invoice_id.in_(list(decided)))
            .group_by(PipelineStep.invoice_id)
        )
        if ms is not None
    )
    if not totals:
        return None
    fastest_ms, fastest_id = totals[0]
    fastest = decided[fastest_id]
    return {
        "fastest_ms": round(fastest_ms, 1),
        "typical_ms": round(totals[len(totals) // 2][0], 1),
        "count": len(totals),
        "label": _label(fastest),
        "number": fastest.invoice_number or "",
        "url": f"/invoices/{fastest.id}",
    }


def _split(invoices: list[Invoice], transcribe: Counter[str]) -> dict[str, float]:
    """The share of invoices that take each branch, from what really happened."""
    split = dict(DEFAULT_SPLIT)
    read = sum(transcribe.values())
    if read:
        split["scan"] = (read - transcribe.get("skipped", 0)) / read
    finished = [invoice for invoice in invoices if invoice.status not in IN_PROGRESS]
    if finished:
        failed = sum(invoice.status == InvoiceStatus.FAILED for invoice in finished)
        auto = sum(_action(invoice) == DecisionAction.AUTO_APPROVE for invoice in finished)
        split["failed"] = failed / len(finished)
        split["auto"] = auto / len(finished)
    resolved = [
        invoice
        for invoice in finished
        if _action(invoice) not in (None, DecisionAction.AUTO_APPROVE)
        and invoice.status != InvoiceStatus.PENDING_REVIEW
    ]
    if resolved:
        approved = sum(invoice.status != InvoiceStatus.REJECTED for invoice in resolved)
        split["review_approved"] = approved / len(resolved)
    return {key: round(value, 4) for key, value in split.items()}


def _counts(invoices: list[Invoice]) -> dict[str, int]:
    statuses = Counter(invoice.status for invoice in invoices)
    return {
        "total": len(invoices),
        "in_flight": sum(statuses[status] for status in IN_PROGRESS),
        "pending_review": statuses[InvoiceStatus.PENDING_REVIEW],
        "approved": statuses[InvoiceStatus.APPROVED],
        "exported": statuses[InvoiceStatus.EXPORTED],
        "rejected": statuses[InvoiceStatus.REJECTED],
        "failed": statuses[InvoiceStatus.FAILED],
        "auto": sum(_action(invoice) == DecisionAction.AUTO_APPROVE for invoice in invoices),
    }


async def _position(session: SessionDep, invoice: Invoice) -> dict[str, Any]:
    """Which desk an in-progress invoice is at: the running step, else the next one."""
    rows = {
        row.step: row
        for row in await session.scalars(
            select(PipelineStep).where(PipelineStep.invoice_id == invoice.id)
        )
    }
    desk = next((key for key, row in rows.items() if row.status == "running"), None)
    if desk is None:
        desk = next((key for key in STEPS if key not in rows), "outcome")
    row = rows.get(desk)
    return {
        "id": str(invoice.id),
        "label": _label(invoice),
        "number": invoice.invoice_number or "",
        "desk": desk,
        "desk_title": STEPS[desk][1],
        "summary": row.summary if row else "",
        "url": f"/invoices/{invoice.id}",
    }


def _outcome(invoice: Invoice) -> dict[str, Any]:
    label, tone = labels.STATUSES.get(invoice.status, (str(invoice.status), "muted"))
    return {
        "id": str(invoice.id),
        "label": _label(invoice),
        "number": invoice.invoice_number or "",
        "status": label,
        "tone": tone,
        "ago": ago(invoice.received_at),
        "url": f"/invoices/{invoice.id}",
    }


def _label(invoice: Invoice) -> str:
    if invoice.vendor is not None:
        return invoice.vendor.name
    return invoice.vendor_name or invoice.original_filename


def _action(invoice: Invoice) -> DecisionAction | None:
    return invoice.decisions[-1].action if invoice.decisions else None


@router.get("/", response_class=HTMLResponse)
async def home_page(request: Request, session: SessionDep) -> HTMLResponse:
    """A simple decision tree: total invoices, then passed, review and stopped."""
    return templates.TemplateResponse(
        request, "home.html", {"w": await workflow_data(session), "nav": "home"}
    )


@router.get("/workflow", response_class=HTMLResponse)
async def workflow_page(request: Request, session: SessionDep) -> HTMLResponse:
    """The live map: moving slips along the whole route."""
    return await _workflow(request, session, pipeline=False)


@router.get("/workflow/pipeline", response_class=HTMLResponse)
async def pipeline_page(request: Request, session: SessionDep) -> HTMLResponse:
    """Every step as cards, drawn by React from the same data."""
    return await _workflow(request, session, pipeline=True)


async def _workflow(request: Request, session: SessionDep, *, pipeline: bool) -> HTMLResponse:
    settings = request.app.state.settings
    vision = settings.vision_model or "not configured"
    return templates.TemplateResponse(
        request,
        "workflow.html",
        {
            "w": await workflow_data(session),
            "nav": "pipeline" if pipeline else "workflow",
            "pipeline": pipeline,
            # Who handles the AI desks, as configured: the map should name the real models.
            "vision_label": f"{vision.split(':')[0]}, local",
            "extract_label": settings.groq_model.split("/")[-1],
            "r": rules_context(settings, await RulesService(session, settings).approval_limit()),
        },
    )


@router.get("/workflow/live")
async def workflow_live(session: SessionDep) -> JSONResponse:
    return JSONResponse(await workflow_data(session), headers={"Cache-Control": "no-store"})
