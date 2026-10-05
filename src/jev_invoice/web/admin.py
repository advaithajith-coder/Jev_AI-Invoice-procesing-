"""Screens for vendors, purchase orders, and exports.

Same pattern as the review screens: plain forms that post and redirect, with errors shown
on the page. The name typed into a form is remembered (cookie) and recorded in the audit
trail with every change.
"""

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from jev_invoice.api.dependencies import (
    SessionDep,
    get_export_service,
    get_importer,
    get_pipeline,
    get_po_service,
    get_vendor_service,
)
from jev_invoice.core.errors import AppError
from jev_invoice.db.models import AuditEvent, Invoice
from jev_invoice.domain.enums import GLAccountType, PurchaseOrderStatus, VendorStatus
from jev_invoice.export.formats import FORMATS
from jev_invoice.export.service import ExportService
from jev_invoice.masterdata.accounts import AccountCreate, AccountFields, AccountService
from jev_invoice.masterdata.importing import (
    MAX_BYTES,
    PO_TEMPLATE,
    VENDOR_TEMPLATE,
    CsvImporter,
)
from jev_invoice.masterdata.purchase_orders import (
    POCreate,
    POLineIn,
    PurchaseOrderService,
    ReceiptCreate,
    today_utc,
)
from jev_invoice.masterdata.rules import RulesService
from jev_invoice.masterdata.vendors import (
    BankAccountChange,
    VendorCreate,
    VendorFields,
    VendorService,
)
from jev_invoice.pipeline.recheck import recheck_waiting_on
from jev_invoice.pipeline.service import InvoicePipeline
from jev_invoice.web.routes import REVIEWER_COOKIE, templates

router = APIRouter(include_in_schema=False)
VendorsDep = Annotated[VendorService, Depends(get_vendor_service)]
POsDep = Annotated[PurchaseOrderService, Depends(get_po_service)]
ImporterDep = Annotated[CsvImporter, Depends(get_importer)]
ExportsDep = Annotated[ExportService, Depends(get_export_service)]

NOTICES = {
    "vendor_created": "Vendor added.",
    "vendor_updated": "Vendor saved.",
    "bank_changed": "Bank account changed and recorded in the history.",
    "po_created": "Purchase order created.",
    "delivery": "Delivery recorded. Invoices waiting on this order were checked again.",
    "po_closed": "Purchase order closed.",
    "po_cancelled": "Purchase order cancelled.",
    "account_created": "Account added. Jev can now code invoice lines to it.",
    "account_updated": "Account saved.",
    "settings_saved": "Saved. Invoices processed from now on use the new limit.",
}


class FormError(AppError):
    status_code = 400
    code = "invalid_form"


def _person(name: str) -> str:
    name = name.strip()[:100]
    if not name:
        raise FormError("Enter your name so the change is recorded against you.")
    return name


def _message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors()
        )
    if isinstance(exc, AppError):
        return exc.message
    return str(exc)


def _decimal_or_none(value: str) -> Decimal | None:
    text = value.strip().rstrip("%")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"'{value}' is not a number") from exc


def _redirect(url: str, person: str | None = None) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if person:
        response.set_cookie(
            REVIEWER_COOKIE, person, max_age=365 * 86400, httponly=True, samesite="lax"
        )
    return response


def _render(
    request: Request, name: str, context: dict[str, Any], status: int = 200
) -> HTMLResponse:
    context.setdefault("person", request.cookies.get(REVIEWER_COOKIE, ""))
    context.setdefault("notice", NOTICES.get(request.query_params.get("notice", "")))
    return templates.TemplateResponse(request, name, context, status_code=status)


async def _history(session: SessionDep, entity_id: uuid.UUID) -> list[AuditEvent]:
    return list(
        await session.scalars(
            select(AuditEvent).where(AuditEvent.entity_id == str(entity_id)).order_by(AuditEvent.id)
        )
    )


# ── Vendors ───────────────────────────────────────────────────────────────────


async def _vendors_context(vendors: VendorService) -> dict[str, Any]:
    return {
        "vendors": await vendors.list_all(),
        "open_pos": await vendors.open_po_counts(),
        "gl_accounts": await vendors.gl_accounts(),
        "statuses": list(VendorStatus),
        "nav": "vendors",
    }


@router.get("/vendors", response_class=HTMLResponse)
async def vendors_page(request: Request, vendors: VendorsDep) -> HTMLResponse:
    return _render(request, "vendors.html", await _vendors_context(vendors))


def _csv_template(content: str, filename: str) -> Response:
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Declared before /vendors/{vendor_id} and /purchase-orders/{po_id} so they match first.
@router.get("/vendors/import-template.csv")
async def vendor_template() -> Response:
    return _csv_template(VENDOR_TEMPLATE, "vendors-template.csv")


@router.get("/purchase-orders/import-template.csv")
async def po_template() -> Response:
    return _csv_template(PO_TEMPLATE, "purchase-orders-template.csv")


@router.post("/vendors", response_model=None)
async def add_vendor(
    request: Request,
    vendors: VendorsDep,
    session: SessionDep,
    name: Annotated[str, Form()] = "",
    default_currency: Annotated[str, Form()] = "USD",
    payment_terms_days: Annotated[str, Form()] = "30",
    early_pay_discount_pct: Annotated[str, Form()] = "",
    early_pay_days: Annotated[str, Form()] = "",
    po_required: Annotated[str, Form()] = "",
    default_gl_account_code: Annotated[str, Form()] = "",
    bank_account: Annotated[str, Form()] = "",
    email_domain: Annotated[str, Form()] = "",
    tax_id: Annotated[str, Form()] = "",
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        actor = _person(person)
        data = VendorCreate(
            name=name,
            default_currency=default_currency,
            payment_terms_days=int(payment_terms_days or 30),
            early_pay_discount_pct=_decimal_or_none(early_pay_discount_pct),
            early_pay_days=int(early_pay_days) if early_pay_days.strip() else None,
            po_required=po_required == "on",
            default_gl_account_code=default_gl_account_code,
            bank_account=bank_account or None,
            email_domain=email_domain or None,
            tax_id=tax_id or None,
        )
        vendor = await vendors.create(data, actor=actor)
    except (AppError, ValidationError, ValueError) as exc:
        await session.rollback()
        context = await _vendors_context(vendors)
        context.update(
            error=_message(exc),
            person=person,
            form={
                "name": name,
                "default_currency": default_currency,
                "payment_terms_days": payment_terms_days,
                "early_pay_discount_pct": early_pay_discount_pct,
                "early_pay_days": early_pay_days,
                "po_required": po_required == "on",
                "default_gl_account_code": default_gl_account_code,
                "email_domain": email_domain,
                "tax_id": tax_id,
            },
        )
        return _render(request, "vendors.html", context, 400)
    return _redirect(f"/vendors/{vendor.id}?notice=vendor_created", actor)


@router.post("/vendors/import", response_model=None)
async def import_vendors(
    request: Request,
    file: UploadFile,
    importer: ImporterDep,
    vendors: VendorsDep,
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        actor = _person(person)
    except AppError as exc:
        context = await _vendors_context(vendors)
        context.update(import_errors=[exc.message])
        return _render(request, "vendors.html", context, 400)
    report = await importer.import_vendors(await file.read(MAX_BYTES + 1), actor=actor)
    context = await _vendors_context(vendors)
    context.update(import_errors=report.errors, imported=report.created if report.ok else None)
    return _render(request, "vendors.html", context, 200 if report.ok else 422)


async def _vendor_context(
    session: SessionDep, vendors: VendorService, vendor_id: uuid.UUID
) -> dict[str, Any]:
    vendor = await vendors.get(vendor_id)
    invoices = list(
        await session.scalars(
            select(Invoice)
            .where(Invoice.vendor_id == vendor.id)
            .order_by(Invoice.received_at.desc())
            .limit(20)
        )
    )
    return {
        "vendor": vendor,
        "invoices": invoices,
        "events": await _history(session, vendor.id),
        "gl_accounts": await vendors.gl_accounts(),
        "statuses": list(VendorStatus),
        "nav": "vendors",
    }


@router.get("/vendors/{vendor_id}", response_class=HTMLResponse)
async def vendor_page(
    request: Request, vendor_id: uuid.UUID, session: SessionDep, vendors: VendorsDep
) -> HTMLResponse:
    return _render(request, "vendor.html", await _vendor_context(session, vendors, vendor_id))


@router.post("/vendors/{vendor_id}", response_model=None)
async def save_vendor(
    request: Request,
    vendor_id: uuid.UUID,
    session: SessionDep,
    vendors: VendorsDep,
    name: Annotated[str, Form()] = "",
    default_currency: Annotated[str, Form()] = "USD",
    payment_terms_days: Annotated[str, Form()] = "30",
    early_pay_discount_pct: Annotated[str, Form()] = "",
    early_pay_days: Annotated[str, Form()] = "",
    po_required: Annotated[str, Form()] = "",
    default_gl_account_code: Annotated[str, Form()] = "",
    email_domain: Annotated[str, Form()] = "",
    tax_id: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "active",
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        actor = _person(person)
        data = VendorFields(
            name=name,
            default_currency=default_currency,
            payment_terms_days=int(payment_terms_days or 30),
            early_pay_discount_pct=_decimal_or_none(early_pay_discount_pct),
            early_pay_days=int(early_pay_days) if early_pay_days.strip() else None,
            po_required=po_required == "on",
            default_gl_account_code=default_gl_account_code,
            email_domain=email_domain or None,
            tax_id=tax_id or None,
            status=VendorStatus(status),
        )
        await vendors.update(vendor_id, data, actor=actor)
    except (AppError, ValidationError, ValueError) as exc:
        await session.rollback()
        context = await _vendor_context(session, vendors, vendor_id)
        context.update(error=_message(exc), person=person)
        return _render(request, "vendor.html", context, 400)
    return _redirect(f"/vendors/{vendor_id}?notice=vendor_updated", actor)


@router.post("/vendors/{vendor_id}/bank-account", response_model=None)
async def change_bank(
    request: Request,
    vendor_id: uuid.UUID,
    session: SessionDep,
    vendors: VendorsDep,
    bank_account: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        change = BankAccountChange(bank_account=bank_account, changed_by=_person(person), note=note)
        await vendors.change_bank_account(vendor_id, change)
    except (AppError, ValidationError) as exc:
        await session.rollback()
        context = await _vendor_context(session, vendors, vendor_id)
        message = _message(exc)
        if "note" in message:
            message = (
                "Write a note (at least 10 characters) saying how the new account was verified."
            )
        context.update(bank_error=message, person=person, bank_note=note)
        return _render(request, "vendor.html", context, 400)
    return _redirect(f"/vendors/{vendor_id}?notice=bank_changed", person.strip())


# ── Purchase orders ───────────────────────────────────────────────────────────


@router.get("/purchase-orders", response_class=HTMLResponse)
async def purchase_orders_page(request: Request, pos: POsDep, vendors: VendorsDep) -> HTMLResponse:
    return _render(
        request,
        "purchase_orders.html",
        {
            "summaries": await pos.list_all(),
            "vendors": await vendors.list_all(),
            "nav": "pos",
            "today": today_utc(),
        },
    )


def _parse_lines(text: str) -> list[POLineIn]:
    """One line per item: description | quantity | unit price | GL code (optional)."""
    lines = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) < 3:
            raise FormError(f"Line {number}: use 'description | quantity | unit price | GL code'.")
        try:
            quantity = Decimal(parts[1].replace(",", ""))
            price = Decimal(parts[2].replace(",", "").lstrip("£$€"))
        except InvalidOperation as exc:
            raise FormError(f"Line {number}: quantity and unit price must be numbers.") from exc
        lines.append(
            POLineIn(
                description=parts[0],
                quantity=quantity,
                unit_price=price,
                gl_account_code=parts[3] if len(parts) > 3 and parts[3] else None,
            )
        )
    if not lines:
        raise FormError("Add at least one line.")
    return lines


@router.post("/purchase-orders", response_model=None)
async def create_po(
    request: Request,
    session: SessionDep,
    pos: POsDep,
    vendors: VendorsDep,
    po_number: Annotated[str, Form()] = "",
    vendor_id: Annotated[str, Form()] = "",
    issued_date: Annotated[str, Form()] = "",
    currency: Annotated[str, Form()] = "",
    lines: Annotated[str, Form()] = "",
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        actor = _person(person)
        data = POCreate(
            po_number=po_number,
            vendor_id=uuid.UUID(vendor_id) if vendor_id else uuid.UUID(int=0),
            issued_date=date.fromisoformat(issued_date) if issued_date else today_utc(),
            currency=currency or None,
            lines=_parse_lines(lines),
        )
        po = await pos.create(data, actor=actor)
    except (AppError, ValidationError, ValueError) as exc:
        await session.rollback()
        return _render(
            request,
            "purchase_orders.html",
            {
                "summaries": await pos.list_all(),
                "vendors": await vendors.list_all(),
                "nav": "pos",
                "today": today_utc(),
                "error": _message(exc),
                "form": {
                    "po_number": po_number,
                    "vendor_id": vendor_id,
                    "issued_date": issued_date,
                    "currency": currency,
                    "lines": lines,
                },
                "person": person,
            },
            400,
        )
    return _redirect(f"/purchase-orders/{po.id}?notice=po_created", actor)


@router.post("/purchase-orders/import", response_model=None)
async def import_pos(
    request: Request,
    file: UploadFile,
    importer: ImporterDep,
    pos: POsDep,
    vendors: VendorsDep,
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        actor = _person(person)
        report = await importer.import_purchase_orders(await file.read(MAX_BYTES + 1), actor=actor)
        errors, created = report.errors, report.created
    except AppError as exc:
        errors, created = [exc.message], 0
    return _render(
        request,
        "purchase_orders.html",
        {
            "summaries": await pos.list_all(),
            "vendors": await vendors.list_all(),
            "nav": "pos",
            "today": today_utc(),
            "import_errors": errors,
            "imported": created if not errors else None,
        },
        200 if not errors else 422,
    )


async def _po_context(
    session: SessionDep, pos: PurchaseOrderService, po_id: uuid.UUID
) -> dict[str, Any]:
    summary = await pos.get(po_id)
    return {
        "s": summary,
        "po": summary.po,
        "invoices": await pos.invoices_for(po_id),
        "events": await _history(session, po_id),
        "today": today_utc(),
        "closable": summary.po.status
        not in (PurchaseOrderStatus.CLOSED, PurchaseOrderStatus.CANCELLED),
        "nav": "pos",
    }


@router.get("/purchase-orders/{po_id}", response_class=HTMLResponse)
async def po_page(
    request: Request, po_id: uuid.UUID, session: SessionDep, pos: POsDep
) -> HTMLResponse:
    return _render(request, "purchase_order.html", await _po_context(session, pos, po_id))


@router.post("/purchase-orders/{po_id}/receipts", response_model=None)
async def record_delivery(
    request: Request,
    po_id: uuid.UUID,
    session: SessionDep,
    pos: POsDep,
    pipeline: InvoicePipeline = Depends(get_pipeline),  # noqa: B008 - FastAPI dependency
) -> Response:
    form = await request.form()
    person = str(form.get("person", ""))
    try:
        quantities: dict[uuid.UUID, Decimal] = {}
        for key, value in form.items():
            if key.startswith("qty_") and str(value).strip():
                try:
                    quantities[uuid.UUID(key[4:])] = Decimal(str(value).replace(",", ""))
                except (ValueError, InvalidOperation) as exc:
                    raise FormError("Received quantities must be numbers.") from exc
        received = str(form.get("received_date", "")) or today_utc().isoformat()
        data = ReceiptCreate(
            received_date=date.fromisoformat(received),
            received_by=_person(person),
            quantities=quantities,
        )
        receipt = await pos.record_receipt(po_id, data)
    except (AppError, ValidationError, ValueError) as exc:
        await session.rollback()
        context = await _po_context(session, pos, po_id)
        context.update(error=_message(exc), person=person)
        return _render(request, "purchase_order.html", context, 400)
    rechecked = await recheck_waiting_on(
        session, pipeline, po_id, actor=data.received_by, why=f"delivery {receipt.receipt_number}"
    )
    approved = f"&approved={','.join(rechecked.approved)}" if rechecked.approved else ""
    return _redirect(f"/purchase-orders/{po_id}?notice=delivery{approved}", person.strip())


@router.post("/purchase-orders/{po_id}/status", response_model=None)
async def change_po_status(
    request: Request,
    po_id: uuid.UUID,
    session: SessionDep,
    pos: POsDep,
    status: Annotated[str, Form(pattern="^(closed|cancelled)$")],
    note: Annotated[str, Form()] = "",
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        actor = _person(person)
        await pos.set_status(po_id, PurchaseOrderStatus(status), actor=actor, note=note or None)
    except AppError as exc:
        await session.rollback()
        context = await _po_context(session, pos, po_id)
        context.update(error=exc.message, person=person)
        return _render(request, "purchase_order.html", context, max(exc.status_code, 400))
    return _redirect(f"/purchase-orders/{po_id}?notice=po_{status}", actor)


# ── Chart of accounts ─────────────────────────────────────────────────────────


async def _accounts_context(session: SessionDep) -> dict[str, Any]:
    usage = await AccountService(session).list_with_usage()
    return {
        "usage": usage,
        "types": list(GLAccountType),
        "total": sum((item.amount for item in usage), Decimal(0)),
        "nav": "accounts",
    }


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, session: SessionDep) -> HTMLResponse:
    return _render(request, "accounts.html", await _accounts_context(session))


@router.post("/accounts", response_model=None)
async def add_account(
    request: Request,
    session: SessionDep,
    code: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    account_type: Annotated[str, Form()] = "expense",
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        actor = _person(person)
        data = AccountCreate(
            code=code, name=name, description=description, account_type=account_type
        )
        account = await AccountService(session).create(data, actor=actor)
    except (AppError, ValidationError, ValueError) as exc:
        await session.rollback()
        context = await _accounts_context(session)
        context.update(
            error=_message(exc),
            person=person,
            form={
                "code": code,
                "name": name,
                "description": description,
                "account_type": account_type,
            },
        )
        return _render(request, "accounts.html", context, 400)
    return _redirect(f"/accounts/{account.code}?notice=account_created", actor)


async def _account_context(session: SessionDep, code: str) -> dict[str, Any]:
    service = AccountService(session)
    return {
        "account": await service.get(code),
        "lines": await service.recent_lines(code),
        "types": list(GLAccountType),
        "events": list(
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.entity_type == "gl_account", AuditEvent.entity_id == code)
                .order_by(AuditEvent.id)
            )
        ),
        "nav": "accounts",
    }


@router.get("/accounts/{code}", response_class=HTMLResponse)
async def account_page(request: Request, code: str, session: SessionDep) -> HTMLResponse:
    return _render(request, "account.html", await _account_context(session, code))


@router.post("/accounts/{code}", response_model=None)
async def save_account(
    request: Request,
    code: str,
    session: SessionDep,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    account_type: Annotated[str, Form()] = "expense",
    is_active: Annotated[str, Form()] = "",
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        actor = _person(person)
        data = AccountFields(
            name=name,
            description=description,
            account_type=account_type,
            is_active=is_active == "on",
        )
        await AccountService(session).update(code, data, actor=actor)
    except (AppError, ValidationError, ValueError) as exc:
        await session.rollback()
        context = await _account_context(session, code)
        context.update(error=_message(exc), person=person)
        return _render(request, "account.html", context, 400)
    return _redirect(f"/accounts/{code}?notice=account_updated", actor)


# ── Settings ──────────────────────────────────────────────────────────────────


async def _settings_context(request: Request, session: SessionDep) -> dict[str, Any]:
    settings = request.app.state.settings
    rules = RulesService(session, settings)
    events = list(
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_type == "setting")
            .order_by(AuditEvent.id.desc())
            .limit(20)
        )
    )
    return {
        "approval_limit": await rules.approval_limit(),
        "approval_setting": await rules.approval_limit_setting(),
        "default_limit": settings.approval_limit,
        "currency": settings.base_currency,
        "events": events,
        "nav": "settings",
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: SessionDep) -> HTMLResponse:
    return _render(request, "settings.html", await _settings_context(request, session))


@router.post("/settings/approval-limit", response_model=None)
async def save_approval_limit(
    request: Request,
    session: SessionDep,
    approval_limit: Annotated[str, Form()] = "",
    person: Annotated[str, Form()] = "",
) -> Response:
    try:
        actor = _person(person)
        await RulesService(session, request.app.state.settings).set_approval_limit(
            approval_limit, actor=actor
        )
    except AppError as exc:
        await session.rollback()
        context = await _settings_context(request, session)
        context.update(error=exc.message, person=person, typed=approval_limit)
        return _render(request, "settings.html", context, 400)
    return _redirect("/settings?notice=settings_saved", actor)


# ── Exports ───────────────────────────────────────────────────────────────────


async def _exports_context(exports: ExportService) -> dict[str, Any]:
    ready, not_ready = await exports.candidates()
    return {
        "ready": ready,
        "not_ready": not_ready,
        "batches": await exports.batches(),
        "formats": FORMATS,
        "nav": "exports",
    }


@router.get("/exports", response_class=HTMLResponse)
async def exports_page(request: Request, exports: ExportsDep) -> HTMLResponse:
    context = await _exports_context(exports)
    created = request.query_params.get("created")
    context["created"] = next((b for b in context["batches"] if str(b.id) == created), None)
    return _render(request, "exports.html", context)


@router.post("/exports", response_model=None)
async def create_export(request: Request, session: SessionDep, exports: ExportsDep) -> Response:
    form = await request.form()
    person = str(form.get("person", ""))
    try:
        actor = _person(person)
        ids = [uuid.UUID(str(value)) for value in form.getlist("invoice_id")]
        batch = await exports.create_batch(str(form.get("format", "")), ids, actor=actor)
    except (AppError, ValueError) as exc:
        await session.rollback()
        context = await _exports_context(exports)
        context.update(error=_message(exc), person=person)
        return _render(request, "exports.html", context, 400)
    return _redirect(f"/exports?created={batch.id}", actor)


@router.get("/exports/{batch_id}/download")
async def download_export(batch_id: uuid.UUID, exports: ExportsDep) -> Response:
    batch = await exports.batch(batch_id)
    return Response(
        content=batch.content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{batch.filename}"'},
    )
