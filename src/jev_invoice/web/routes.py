"""Review screens: inbox, review queue, and the invoice page where a person decides.

Plain HTML forms that post and redirect (no client framework). Jinja2 autoescaping
is on, so text from invoices (untrusted) is always rendered as text, never markup.
"""

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from jev_invoice.api.dependencies import SessionDep, get_app_settings, get_pipeline
from jev_invoice.api.routes.invoices import PayloadTooLargeError
from jev_invoice.config import Settings
from jev_invoice.core import audit
from jev_invoice.core.errors import AppError, NotFoundError
from jev_invoice.db.models import (
    AuditEvent,
    Decision,
    GLAccount,
    Invoice,
    InvoiceCredit,
    ReviewTask,
)
from jev_invoice.decision.policy import DEFAULT_THRESHOLDS
from jev_invoice.domain.enums import ActionBand, DecisionAction, InvoiceStatus, ReviewStatus
from jev_invoice.ingest.parsing import parse_document
from jev_invoice.ingest.storage import LocalFileStorage
from jev_invoice.payables.service import (
    DISPUTE_CATEGORIES,
    PAYABLE_STATUSES,
    PAYMENT_METHODS,
    RELEASE_OUTCOMES,
    PayablesService,
    invoice_balance,
    invoice_terms,
    is_credit_note,
    payment_plan,
)
from jev_invoice.pipeline.review import ReviewService
from jev_invoice.pipeline.service import InvoicePipeline, invoice_query
from jev_invoice.web import labels
from jev_invoice.web.lifecycle import lifecycle
from jev_invoice.web.steps import load_steps

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

REVIEWER_COOKIE = "reviewer"
BROWSER_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}  # TIFF does not display


class FormError(AppError):
    status_code = 400
    code = "invalid_form"


NOTICES = {
    "uploaded": "Invoice processed.",
    "duplicate": "This exact file was uploaded before. Showing the existing invoice.",
    "approved": "Approved and recorded.",
    "accounts_saved": "Accounts saved and recorded in the history.",
    "rejected": "Rejected and recorded.",
}


# ── Formatting helpers available in every template ───────────────────────────


def money(value: Decimal | None, currency: str | None = None) -> str:
    if value is None:
        return ""
    amount = f"{value:,.2f}"
    return f"{currency} {amount}" if currency else amount


def nice_date(value: Any) -> str:
    return value.strftime("%d %b %Y").lstrip("0") if value else ""


def ago(value: datetime | None) -> str:
    if value is None:
        return ""
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)  # SQLite returns naive UTC
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count > 1 else ''} ago"
    return "just now"


def percent(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.0f}%"


def quantity(value: Decimal | None) -> str:
    """12.0000 -> '12', 2.5000 -> '2.5' (without normalize()'s 1E+1 surprises)."""
    if value is None:
        return ""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def duration(ms: object) -> str:
    """320 -> '320 ms', 1734 -> '1.7 s', 75000 -> '1 min 15 s'. Anything else -> ''."""
    if not isinstance(ms, int | float) or isinstance(ms, bool):
        return ""
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)} min {seconds % 60:.0f} s"


def rate(value: Decimal) -> str:
    """An exchange rate without trailing zeros: 0.01038000 -> 0.01038."""
    return f"{value.normalize():f}"


templates.env.filters.update(
    rate=rate, money=money, date=nice_date, ago=ago, pct=percent, qty=quantity, duration=duration
)
# Changes when any stylesheet or script changes, so browsers fetch the new version.
_STATIC = Path(__file__).parent / "static"
ASSET_VERSION = max(int(path.stat().st_mtime) for path in _STATIC.iterdir() if path.is_file())

_LEGAL_SUFFIX = re.compile(r",?\s+(inc|llc|ltd|llp|corp|co|plc|gmbh)\.?$", re.IGNORECASE)


def use_company(name: str, base_currency: str = "USD") -> None:
    """Name the screens after the company they process invoices for, and its currency."""
    templates.env.globals.update(
        company=name, company_short=_LEGAL_SUFFIX.sub("", name), base_currency=base_currency
    )


use_company(Settings.model_fields["company_name"].default)
templates.env.globals.update(
    asset_version=ASSET_VERSION,
    reason=labels.reason,
    ACTIONS=labels.ACTIONS,
    BANDS=labels.BANDS,
    STATUSES=labels.STATUSES,
    AUDIT=labels.AUDIT_ACTIONS,
    DOC_TYPES=labels.DOCUMENT_TYPES,
    VENDOR_STATUSES=labels.VENDOR_STATUSES,
    PO_STATUSES=labels.PO_STATUSES,
    DISPUTE_CATEGORIES=DISPUTE_CATEGORIES,
    RELEASE_OUTCOMES=RELEASE_OUTCOMES,
    PAYMENT_METHODS=PAYMENT_METHODS,
    event_detail=labels.event_detail,
    lifecycle=lifecycle,
)


# ── Inbox ─────────────────────────────────────────────────────────────────────


async def _inbox_context(session: SessionDep, status: InvoiceStatus | None) -> dict[str, Any]:
    query = invoice_query().order_by(Invoice.received_at.desc()).limit(200)
    if status is not None:
        query = query.where(Invoice.status == status)
    counts_rows = await session.execute(
        select(Invoice.status, func.count()).group_by(Invoice.status)
    )
    counts = {InvoiceStatus(row[0]): row[1] for row in counts_rows}
    return {
        "invoices": list(await session.scalars(query)),
        "counts": counts,
        "total": sum(counts.values()),
        "status_filter": status,
        "nav": "inbox",
    }


@router.get("/", response_class=HTMLResponse)
async def inbox(
    request: Request,
    session: SessionDep,
    status: InvoiceStatus | None = None,
) -> HTMLResponse:
    context = await _inbox_context(session, status)
    return templates.TemplateResponse(request, "inbox.html", context)


@router.post("/upload", response_model=None)
async def upload(
    request: Request,
    file: UploadFile,
    session: SessionDep,
    pipeline: Annotated[InvoicePipeline, Depends(get_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Response:
    try:
        data = await file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise PayloadTooLargeError(
                f"That file is larger than {settings.max_upload_bytes // (1024 * 1024)} MB."
            )
        result = await pipeline.receive(data, file.filename or "upload", file.content_type)
        if not result.already_received:
            runner = request.app.state.runner
            if runner is not None:
                runner.submit(result.invoice_id)  # watch the steps live on its page
            else:
                await pipeline.process(result.invoice_id)
    except AppError as exc:
        await session.rollback()
        context = await _inbox_context(session, None)
        context["error"] = exc.message
        return templates.TemplateResponse(
            request, "inbox.html", context, status_code=max(exc.status_code, 400)
        )
    suffix = "?notice=duplicate" if result.already_received else ""
    return RedirectResponse(f"/invoices/{result.invoice_id}{suffix}", status_code=303)


# ── Review queue ──────────────────────────────────────────────────────────────


@router.get("/review", response_class=HTMLResponse)
async def review_queue(request: Request, session: SessionDep, show: str = "all") -> HTMLResponse:
    """Open reviews, oldest first; optionally only those on hold or disputed."""
    tasks = await ReviewService(session).list_open(limit=200)
    held = [task for task in tasks if task.invoice.hold_kind]
    counts = {"held": len(held)}
    if show == "held":
        tasks = held
    else:
        show = "all"
    return templates.TemplateResponse(
        request,
        "review.html",
        {"tasks": tasks, "show": show, "counts": counts, "nav": "review"},
    )


# ── Invoice page ──────────────────────────────────────────────────────────────


async def invoice_context(
    request: Request, session: SessionDep, invoice_id: uuid.UUID
) -> dict[str, Any]:
    session.expire_all()
    invoice = await session.scalar(invoice_query().where(Invoice.id == invoice_id))
    if invoice is None:
        raise NotFoundError("Invoice not found")

    tasks = list(
        await session.scalars(
            select(ReviewTask)
            .where(ReviewTask.invoice_id == invoice.id)
            .order_by(ReviewTask.created_at)
        )
    )
    open_task = next((task for task in tasks if task.status == ReviewStatus.OPEN), None)
    entity_ids = [str(invoice.id), *(str(task.id) for task in tasks)]
    events = list(
        await session.scalars(
            select(AuditEvent).where(AuditEvent.entity_id.in_(entity_ids)).order_by(AuditEvent.id)
        )
    )
    decision = invoice.decisions[-1] if invoice.decisions else None
    reviewer = request.cookies.get(REVIEWER_COOKIE, "")
    today = datetime.now(UTC).date()
    payables = PayablesService(session)
    credit_note = is_credit_note(invoice)
    balance = invoice_balance(invoice)
    applied_to = (
        await session.scalar(
            select(InvoiceCredit)
            .where(InvoiceCredit.credit_note_id == invoice.id)
            .options(selectinload(InvoiceCredit.invoice))
        )
        if credit_note
        else None
    )
    jev_rows = _jev_rows(decision.jev_answers if decision else None)
    gl_accounts = list(
        await session.scalars(select(GLAccount).where(GLAccount.is_active).order_by(GLAccount.code))
    )
    steps = await load_steps(
        session,
        invoice,
        decision=decision,
        open_task=open_task,
        jev_rows=jev_rows,
        account_names={account.code: account.name for account in gl_accounts},
    )
    return {
        "balance": balance,
        "is_credit_note": credit_note,
        "payable": invoice.status in PAYABLE_STATUSES and not credit_note,
        "plan": payment_plan(invoice, today),
        "credit_notes": await payables.unapplied_credit_notes(invoice),
        "credit_targets": (
            await payables.credit_targets(invoice)
            if credit_note and open_task and applied_to is None
            else []
        ),
        "applied_to": applied_to,
        "uncoded": invoice.status != InvoiceStatus.EXPORTED
        and any(not line.gl_account_code for line in invoice.lines),
        "dispute_message": _dispute_message(request, invoice) if invoice.hold_kind else None,
        "form": {},
        "invoice": invoice,
        "decision": decision,
        "open_task": open_task,
        "terms": invoice_terms(invoice),
        "today": today,
        "review_warnings": (
            await review_warnings(session, invoice, decision, reviewer) if open_task else []
        ),
        "events": events,
        "jev_rows": jev_rows,
        "st": steps,
        "document_text": _document_text(request, invoice),
        "is_pdf": invoice.content_type == "application/pdf",
        "is_image": invoice.content_type in BROWSER_IMAGE_TYPES,
        "proposes_reject": bool(decision and decision.action == DecisionAction.REJECT_DOCUMENT),
        "reviewer": request.cookies.get(REVIEWER_COOKIE, ""),
        "gl_accounts": gl_accounts,
        "notice": NOTICES.get(request.query_params.get("notice", "")),
        "nav": "review" if open_task else "inbox",
    }


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_page(
    request: Request, invoice_id: uuid.UUID, session: SessionDep
) -> HTMLResponse:
    context = await invoice_context(request, session, invoice_id)
    return templates.TemplateResponse(request, "invoice.html", context)


@router.get("/invoices/{invoice_id}/steps", response_class=HTMLResponse)
async def invoice_steps(
    request: Request, invoice_id: uuid.UUID, session: SessionDep
) -> HTMLResponse:
    """Just the steps, polled every second while the invoice is processed."""
    context = await invoice_context(request, session, invoice_id)
    return templates.TemplateResponse(request, "_steps.html", context)


@router.get("/invoices/{invoice_id}/journey")
async def old_journey_page(invoice_id: uuid.UUID) -> RedirectResponse:
    """The journey is now the steps on the invoice page."""
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=308)


@router.post("/invoices/{invoice_id}/review", response_model=None)
async def submit_review(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    task_id: Annotated[uuid.UUID, Form()],
    verdict: Annotated[str, Form(pattern="^(approve|reject)$")],
    reviewer: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> Response:
    reviewer = reviewer.strip()[:100]
    try:
        if not reviewer:
            raise FormError("Enter your name so the decision is recorded against you.")
        invoice = await session.get(Invoice, invoice_id)
        decision = await session.scalar(
            select(Decision)
            .where(Decision.invoice_id == invoice_id)
            .order_by(Decision.created_at.desc())
        )
        warnings = await review_warnings(session, invoice, decision, reviewer) if invoice else []
        await ReviewService(session).resolve(
            task_id,
            approve=verdict == "approve",
            reviewer=reviewer,
            note=note[:2000] or None,
            warnings=warnings if verdict == "approve" else None,
        )
    except AppError as exc:
        await session.rollback()
        context = await invoice_context(request, session, invoice_id)
        context.update(error=exc.message, reviewer=reviewer, note=note)
        return templates.TemplateResponse(
            request, "invoice.html", context, status_code=max(exc.status_code, 400)
        )
    notice = "approved" if verdict == "approve" else "rejected"
    response = RedirectResponse(f"/invoices/{invoice_id}?notice={notice}", status_code=303)
    response.set_cookie(
        REVIEWER_COOKIE, reviewer, max_age=365 * 86400, httponly=True, samesite="lax"
    )
    return response


@router.post("/invoices/{invoice_id}/accounts", response_model=None)
async def save_accounts(request: Request, invoice_id: uuid.UUID, session: SessionDep) -> Response:
    """A person codes (or recodes) lines to accounts; each change goes into the history."""
    form = await request.form()
    person = str(form.get("person", "")).strip()[:100]
    invoice = await session.scalar(invoice_query().where(Invoice.id == invoice_id))
    if invoice is None:
        raise NotFoundError("Invoice not found")
    try:
        if not person:
            raise FormError("Enter your name so the change is recorded against you.")
        if invoice.status == InvoiceStatus.EXPORTED:
            raise FormError("This invoice is already exported. Recode it in the accounting system.")
        valid = set(await session.scalars(select(GLAccount.code).where(GLAccount.is_active)))
        changes: dict[str, list[str | None]] = {}
        for line in invoice.lines:
            chosen = str(form.get(f"account_{line.id}", "") or "")
            if not chosen or chosen == line.gl_account_code:
                continue
            if chosen not in valid:
                raise FormError(f"Account {chosen} is not an active account.")
            changes[line.description] = [line.gl_account_code, chosen]
            line.gl_account_code = chosen
            line.gl_source = "person"
        if changes:
            audit.record(
                session,
                action="invoice.accounts_changed",
                entity_type="invoice",
                entity_id=invoice.id,
                actor=person,
                data={"changes": changes},
            )
        await session.commit()
    except AppError as exc:
        await session.rollback()
        context = await invoice_context(request, session, invoice_id)
        context.update(error=exc.message, reviewer=person)
        return templates.TemplateResponse(
            request, "invoice.html", context, status_code=max(exc.status_code, 400)
        )
    response = RedirectResponse(f"/invoices/{invoice_id}?notice=accounts_saved", status_code=303)
    response.set_cookie(REVIEWER_COOKIE, person, max_age=365 * 86400, httponly=True, samesite="lax")
    return response


@router.get("/invoices/{invoice_id}/document")
async def original_document(
    request: Request, invoice_id: uuid.UUID, session: SessionDep
) -> Response:
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None:
        raise NotFoundError("Invoice not found")
    storage: LocalFileStorage = request.app.state.storage
    data = storage.read(invoice.storage_key)
    inline_types = {"application/pdf", *BROWSER_IMAGE_TYPES, "image/tiff"}
    media_type = (
        invoice.content_type
        if invoice.content_type in inline_types
        else "text/plain; charset=utf-8"
    )
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(invoice.original_filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        },
    )


# ── helpers ───────────────────────────────────────────────────────────────────


async def review_warnings(
    session: SessionDep, invoice: Invoice, decision: Decision | None, reviewer: str
) -> list[str]:
    """Controls a real AP team enforces that this demo only warns about."""
    warnings = [
        "Demo: anyone can approve. In a real company your role sets how much you may"
        " approve, and larger invoices go to a more senior approver."
    ]
    if decision is not None and decision.band == ActionBand.BLOCK:
        warnings.append(
            "Overriding a fraud block normally needs a second approver. Here one person can."
        )
    if reviewer and invoice.vendor_id is not None:
        changed = await session.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.entity_id == str(invoice.vendor_id),
                AuditEvent.action == "vendor.bank_account_changed",
                func.lower(AuditEvent.actor) == reviewer.lower(),
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        if changed is not None:
            warnings.append(
                f"You changed this vendor's bank details {ago(changed.occurred_at)}. In real AP,"
                " someone else approves this vendor's invoices (separation of duties)."
            )
    return warnings


def _dispute_message(request: Request, invoice: Invoice) -> str:
    """A message to send the vendor about a dispute (copied by the person; never sent)."""
    settings = request.app.state.settings
    vendor = invoice.vendor.name if invoice.vendor else (invoice.vendor_name or "there")
    number = invoice.invoice_number or invoice.original_filename
    dated = f" dated {nice_date(invoice.invoice_date)}" if invoice.invoice_date else ""
    amount = money(invoice.total, invoice.currency) if invoice.total is not None else ""
    about = DISPUTE_CATEGORIES.get(invoice.hold_category or "", "A query")
    paragraphs = [
        f"Subject: Invoice {number}: query before payment",
        f"Hello {vendor},",
        f"We have put invoice {number}{dated} for {amount} on hold. {about}.",
        invoice.hold_reason or "",
    ]
    if invoice.withheld_amount:
        undisputed = money(invoice_balance(invoice).balance, invoice.currency)
        paragraphs.append(f"We will pay the undisputed part, {undisputed}, on the agreed terms.")
    paragraphs += [
        "Please reply with a corrected invoice, a credit note, or the details that resolve"
        " this, quoting the invoice number.",
        f"Thank you,\n{invoice.held_by or ''}\nAccounts Payable, {settings.company_name}",
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def _document_text(request: Request, invoice: Invoice) -> str | None:
    """Readable text of a non-PDF original (plain text, or an email's body/attachment)."""
    if invoice.content_type == "application/pdf" or invoice.content_type.startswith("image/"):
        return None
    storage: LocalFileStorage = request.app.state.storage
    try:
        data = storage.read(invoice.storage_key)
        return parse_document(data, invoice.original_filename, invoice.content_type).text
    except (AppError, OSError, ValueError):
        return None


def _jev_rows(answers: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Each Jev answer with the policy's verdict on it, for the judgment table."""
    if not answers:
        return []
    t = DEFAULT_THRESHOLDS
    rows: list[dict[str, Any]] = []

    doc_type = answers["document_type"]
    confidence = answers["document_type_confidence"]
    type_label = labels.DOCUMENT_TYPES.get(doc_type, doc_type)
    rows.append(
        {
            "label": "Document type",
            "value": f"{type_label} ({percent(confidence)} sure)",
            "bar": confidence,
            "verdict": _verdict(
                doc_type == "invoice" and confidence >= t.auto_choice_confidence,
                confidence < t.auto_choice_confidence,
            ),
        }
    )
    payable = answers["is_payable"]
    rows.append(
        {
            "label": "Asks for payment",
            "value": percent(payable),
            "bar": payable,
            "verdict": _verdict(payable >= t.auto_is_payable, t.not_payable < payable),
        }
    )
    for key, label in labels.JEV_SIGNALS:
        probability = answers[key]
        present_at = t.injection_signal if key == "prompt_injection" else t.fraud_signal
        rows.append(
            {
                "label": label,
                "value": percent(probability),
                "bar": probability,
                "verdict": (
                    ("flagged", "block")
                    if probability >= present_at
                    else _verdict(probability <= t.risk_clear, True)
                ),
            }
        )
    risk = answers["payment_risk"]
    level = "Low" if risk < t.moderate_risk else "Moderate" if risk < t.high_risk else "High"
    rows.append(
        {
            "label": "Payment risk",
            "value": f"{level} ({risk:.2f} of 2)",
            "bar": risk / 2,
            "verdict": (
                ("clear", "ok")
                if risk < t.moderate_risk
                else ("unsure", "human")
                if risk < t.high_risk
                else ("flagged", "block")
            ),
        }
    )
    return rows


def _verdict(clear: bool, unsure: bool) -> tuple[str, str]:
    if clear:
        return ("clear", "ok")
    if unsure:
        return ("unsure", "human")
    return ("flagged", "confirm")
