"""Invoice page actions beyond approve or reject: hold, dispute, comment, reassign,
short-pay, credit notes, payments, and asking Jev to code lines left uncoded.

Each is a plain form that posts here, is recorded against the person's name, and
redirects back to the invoice with a notice (or re-renders it with the error).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from jev_invoice.api.dependencies import SessionDep
from jev_invoice.coding.service import CodingService
from jev_invoice.core import audit
from jev_invoice.core.errors import AppError
from jev_invoice.db.models import PurchaseOrder
from jev_invoice.payables.service import PayablesError, PayablesService, parse_amount
from jev_invoice.web.routes import (
    NOTICES,
    REVIEWER_COOKIE,
    FormError,
    invoice_context,
    templates,
)

router = APIRouter(include_in_schema=False)

Person = Annotated[str, Form()]

NOTICES.update(
    held="Put on hold. It will not be exported or paid until it is released.",
    disputed="Dispute opened. Send the vendor the message below; the invoice stays on hold.",
    released="Hold released.",
    commented="Comment added.",
    assigned="Review reassigned.",
    short_paid="Short-pay recorded. The balance to pay is reduced.",
    short_pay_cleared="Short-pay removed.",
    credit_applied="Credit applied. The balance to pay is reduced.",
    credit_note_applied="Credit note applied to the invoice and closed.",
    paid="Payment recorded.",
    coded="Lines coded.",
)


async def _act(
    request: Request,
    session: SessionDep,
    invoice_id: uuid.UUID,
    person: str,
    action: Callable[[PayablesService, str], Awaitable[Any]],
    notice: str | Callable[[Any], str],
    *,
    redirect_to: Callable[[Any], uuid.UUID] | None = None,
) -> Response:
    person = person.strip()[:100]
    try:
        if not person:
            raise FormError("Enter your name so this is recorded against you.")
        result = await action(PayablesService(session), person)
    except AppError as exc:
        await session.rollback()
        context = await invoice_context(request, session, invoice_id)
        context.update(error=exc.message, reviewer=person, form=dict(await request.form()))
        return templates.TemplateResponse(
            request, "invoice.html", context, status_code=max(exc.status_code, 400)
        )
    target = redirect_to(result) if redirect_to else invoice_id
    key = notice(result) if callable(notice) else notice
    response = RedirectResponse(f"/invoices/{target}?notice={key}", status_code=303)
    response.set_cookie(REVIEWER_COOKIE, person, max_age=365 * 86400, httponly=True, samesite="lax")
    return response


@router.post("/invoices/{invoice_id}/hold", response_model=None)
async def hold(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    kind: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    person: Person = "",
) -> Response:
    return await _act(
        request,
        session,
        invoice_id,
        person,
        lambda service, actor: service.hold(
            invoice_id, kind=kind, reason=reason, category=category or None, actor=actor
        ),
        "disputed" if kind == "dispute" else "held",
    )


@router.post("/invoices/{invoice_id}/release", response_model=None)
async def release(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    outcome: Annotated[str, Form()] = "other",
    note: Annotated[str, Form()] = "",
    person: Person = "",
) -> Response:
    return await _act(
        request,
        session,
        invoice_id,
        person,
        lambda service, actor: service.release(invoice_id, outcome=outcome, note=note, actor=actor),
        "released",
    )


@router.post("/invoices/{invoice_id}/comment", response_model=None)
async def comment(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    text: Annotated[str, Form()] = "",
    person: Person = "",
) -> Response:
    return await _act(
        request,
        session,
        invoice_id,
        person,
        lambda service, actor: service.comment(invoice_id, text=text, actor=actor),
        "commented",
    )


@router.post("/invoices/{invoice_id}/assign", response_model=None)
async def assign(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    assignee: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    person: Person = "",
) -> Response:
    return await _act(
        request,
        session,
        invoice_id,
        person,
        lambda service, actor: service.assign(
            invoice_id, assignee=assignee, note=note, actor=actor
        ),
        "assigned",
    )


@router.post("/invoices/{invoice_id}/short-pay", response_model=None)
async def short_pay(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    amount: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
    person: Person = "",
) -> Response:
    async def act(service: PayablesService, actor: str) -> Any:
        value = parse_amount(amount or "0", label="Amount to withhold")
        return await service.short_pay(invoice_id, amount=value, reason=reason, actor=actor)

    return await _act(
        request,
        session,
        invoice_id,
        person,
        act,
        lambda invoice: "short_paid" if invoice.withheld_amount else "short_pay_cleared",
    )


@router.post("/invoices/{invoice_id}/credit", response_model=None)
async def apply_credit(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    credit_note_id: Annotated[str, Form()] = "",
    number: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
    person: Person = "",
) -> Response:
    """Apply a credit to this invoice: an uploaded credit note, or one typed in."""

    async def act(service: PayablesService, actor: str) -> Any:
        if credit_note_id:
            return await service.apply_credit(
                invoice_id, actor=actor, credit_note_id=_uuid(credit_note_id), reason=reason
            )
        return await service.apply_credit(
            invoice_id,
            actor=actor,
            number=number,
            amount=parse_amount(amount, label="Credit amount") if amount.strip() else None,
            reason=reason,
        )

    return await _act(request, session, invoice_id, person, act, "credit_applied")


@router.post("/invoices/{credit_note_id}/apply-to", response_model=None)
async def apply_credit_note(
    request: Request,
    credit_note_id: uuid.UUID,
    session: SessionDep,
    target_id: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
    person: Person = "",
) -> Response:
    """From a credit note's page: apply it to one of the vendor's open invoices."""

    async def act(service: PayablesService, actor: str) -> Any:
        if not target_id:
            raise PayablesError("Choose the invoice this credit note is for.")
        return await service.apply_credit(
            _uuid(target_id), actor=actor, credit_note_id=credit_note_id, reason=reason
        )

    return await _act(
        request,
        session,
        credit_note_id,
        person,
        act,
        "credit_note_applied",
        redirect_to=lambda credit: credit.invoice_id,
    )


@router.post("/invoices/{invoice_id}/payment", response_model=None)
async def record_payment(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    paid_on: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    method: Annotated[str, Form()] = "",
    reference: Annotated[str, Form()] = "",
    take_discount: Annotated[str, Form()] = "",
    person: Person = "",
) -> Response:
    async def act(service: PayablesService, actor: str) -> Any:
        return await service.record_payment(
            invoice_id,
            paid_on=_date(paid_on),
            amount=parse_amount(amount, label="Amount paid"),
            take_discount=bool(take_discount),
            method=method,
            reference=reference,
            actor=actor,
        )

    return await _act(request, session, invoice_id, person, act, "paid")


@router.post("/invoices/{invoice_id}/code", response_model=None)
async def code_lines(
    request: Request,
    invoice_id: uuid.UUID,
    session: SessionDep,
    person: Person = "",
) -> Response:
    """Ask Jev to code the lines that have no account yet (invoices from before coding)."""
    state = request.app.state

    async def act(service: PayablesService, actor: str) -> Any:
        invoice = await service.get(invoice_id)
        if invoice.status == "exported":
            raise PayablesError("This invoice is already exported. Code it in accounting.")
        uncoded = [line for line in invoice.lines if not line.gl_account_code]
        if not uncoded:
            raise PayablesError("Every line already has an account.")
        purchase_order = None
        if invoice.purchase_order_id is not None:
            purchase_order = await session.scalar(
                select(PurchaseOrder)
                .options(selectinload(PurchaseOrder.lines))
                .where(PurchaseOrder.id == invoice.purchase_order_id)
            )
        result = await CodingService(
            session,
            state.decisions.jev_client,
            suspense_account=state.settings.export_suspense_account,
            min_confidence=state.settings.coding_min_confidence,
        ).code(
            uncoded,
            vendor=invoice.vendor,
            purchase_order=purchase_order,
            invoice_description=invoice.description,
        )
        audit.record(
            session,
            action="invoice.coded",
            entity_type="invoice",
            entity_id=invoice.id,
            actor=actor,
            data={
                "jev_status": result.jev_status,
                "lines": {line.description: [line.account, line.source] for line in result.lines},
            },
        )
        await session.commit()
        return result

    return await _act(request, session, invoice_id, person, act, "coded")


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise PayablesError("That choice is not valid. Reload the page and try again.") from exc


def _date(value: str) -> date:
    if not value.strip():
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise PayablesError(f"'{value}' is not a date.") from exc
