"""Validated corrections for exact recurring purchases, with bounded scope."""

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.core import audit
from jev_invoice.core.errors import AppError, NotFoundError
from jev_invoice.db.models import GLAccount, Invoice, InvoiceLine, PurchaseCorrection
from jev_invoice.domain.enums import DocumentType, InvoiceStatus


class CorrectionError(AppError):
    status_code = 422
    code = "invalid_correction"


def scope_key(value: str) -> str:
    return hashlib.sha256(" ".join(value.split()).casefold().encode()).hexdigest()


async def validate_value(
    session: AsyncSession, field: str, value: str, source_section: str | None
) -> None:
    from jev_invoice.coding.classification import CATEGORIES
    from jev_invoice.tax.massachusetts import TREATMENTS

    if field == "purchase_category":
        if value not in CATEGORIES or value == "unknown":
            raise CorrectionError("Choose a supported purchase category.")
    elif field == "gl_account":
        account = await session.get(GLAccount, value)
        if (
            not account
            or not account.is_active
            or account.account_type not in {"expense", "cost_of_sales", "asset"}
            or value == "9999"
        ):
            raise CorrectionError("Choose an active purchase account, not suspense.")
    elif field == "tax_treatment":
        treatment = TREATMENTS.get(value)
        if not treatment or treatment[1] is None:
            raise CorrectionError("Choose a supported tax treatment with a configured rate.")
    else:
        raise CorrectionError(
            "Only purchase category, GL account and Massachusetts tax treatment can be reused."
        )


async def propose(
    session: AsyncSession,
    invoice: Invoice,
    line: InvoiceLine,
    *,
    field: str,
    value: str,
    evidence: str,
    person: str,
    valid_from: date,
    valid_until: date,
    source_section: str | None = None,
) -> PurchaseCorrection:
    from jev_invoice.tax.massachusetts import VERSION

    if not person.strip() or not evidence.strip():
        raise CorrectionError(
            "Enter your name and the evidence supporting this specific correction."
        )
    if len(person.strip()) > 100 or len(evidence.strip()) > 2000:
        raise CorrectionError("Keep your name under 100 characters and evidence under 2,000.")
    if not invoice.vendor_id or not invoice.bill_to_name or not invoice.currency:
        raise CorrectionError(
            "Match a vendor and record the buyer and currency before saving a correction."
        )
    if valid_until < valid_from or (valid_until - valid_from).days > 366:
        raise CorrectionError("Choose a validity period of at most one year.")
    await validate_value(session, field, value, source_section)
    if field == "tax_treatment" and invoice.currency != "USD":
        raise CorrectionError("Massachusetts tax corrections require a USD invoice.")
    correction = PurchaseCorrection(
        vendor_id=invoice.vendor_id,
        source_invoice_id=invoice.id,
        source_line_no=line.line_no,
        description=line.description,
        description_key=scope_key(line.description),
        buyer_key=scope_key(invoice.bill_to_name),
        currency=invoice.currency,
        field=field,
        value=value,
        evidence=evidence.strip(),
        created_by=person.strip(),
        source_section=source_section if field == "tax_treatment" else None,
        source_version=VERSION if field == "tax_treatment" else None,
        valid_from=valid_from,
        valid_until=valid_until,
        status="pending",
    )
    session.add(correction)
    await session.flush()
    audit.record(
        session,
        action="correction.proposed",
        entity_type="purchase_correction",
        entity_id=correction.id,
        actor=person.strip(),
        data={
            "field": field,
            "value": value,
            "invoice_id": str(invoice.id),
            "description": line.description,
            "valid_until": str(valid_until),
        },
    )
    return correction


async def set_status(
    session: AsyncSession, key: uuid.UUID, *, activate: bool, person: str, reason: str
) -> PurchaseCorrection:
    from jev_invoice.tax.massachusetts import VERSION

    if not person.strip() or not reason.strip() or len(person) > 100 or len(reason) > 2000:
        raise CorrectionError("Enter your name and a validation or withdrawal reason.")
    row = await session.get(PurchaseCorrection, key)
    if not row:
        raise NotFoundError("Correction not found.")
    if activate:
        if row.status != "pending":
            raise CorrectionError("Only a pending correction can be validated.")
        if row.valid_until < datetime.now(UTC).date():
            raise CorrectionError("This correction has expired; propose a new one.")
        await validate_value(session, row.field, row.value, row.source_section)
        if row.field == "tax_treatment" and row.source_version != VERSION:
            raise CorrectionError("Tax guidance has changed; propose a new correction.")
        # Refuse competing active rules. Do not silently pick the latest decision.
        existing = await session.scalar(
            select(PurchaseCorrection.id).where(
                PurchaseCorrection.vendor_id == row.vendor_id,
                PurchaseCorrection.description_key == row.description_key,
                PurchaseCorrection.buyer_key == row.buyer_key,
                PurchaseCorrection.currency == row.currency,
                PurchaseCorrection.field == row.field,
                PurchaseCorrection.status == "active",
                PurchaseCorrection.valid_from <= row.valid_until,
                PurchaseCorrection.valid_until >= row.valid_from,
            )
        )
        if existing:
            raise CorrectionError(
                "Withdraw the overlapping active correction before validating this one."
            )
    row.status = "active" if activate else "withdrawn"
    row.validated_by = person.strip()
    row.validation_reason = reason.strip()
    audit.record(
        session,
        action="correction.validated" if activate else "correction.withdrawn",
        entity_type="purchase_correction",
        entity_id=row.id,
        actor=person.strip(),
        data={"reason": reason.strip(), "field": row.field, "value": row.value},
    )
    return row


async def matching(
    session: AsyncSession,
    *,
    vendor_id: uuid.UUID | None,
    buyer: str | None,
    currency: str | None,
    invoice_date: date | None,
    lines: Sequence[InvoiceLine],
) -> dict[int, dict[str, PurchaseCorrection]]:
    """Require exact evidence scope and both invoice-date and present-day validity."""
    if not vendor_id or not buyer or not currency or not invoice_date or not lines:
        return {}
    today = datetime.now(UTC).date()
    rows = list(
        await session.scalars(
            select(PurchaseCorrection).where(
                PurchaseCorrection.vendor_id == vendor_id,
                PurchaseCorrection.buyer_key == scope_key(buyer),
                PurchaseCorrection.currency == currency,
                PurchaseCorrection.status == "active",
                PurchaseCorrection.valid_from <= invoice_date,
                PurchaseCorrection.valid_until >= invoice_date,
                PurchaseCorrection.valid_from <= today,
                PurchaseCorrection.valid_until >= today,
                PurchaseCorrection.description_key.in_(
                    {scope_key(line.description) for line in lines}
                ),
            )
        )
    )
    result: dict[int, dict[str, PurchaseCorrection]] = {}
    for line in lines:
        candidates: dict[str, list[PurchaseCorrection]] = {}
        for row in rows:
            if row.description_key == scope_key(line.description):
                candidates.setdefault(row.field, []).append(row)
        for field, choices in candidates.items():
            if len(choices) != 1:
                continue  # Concurrent or legacy overlapping rules require ordinary review.
            row = choices[0]
            try:
                await validate_value(session, field, row.value, row.source_section)
            except CorrectionError:
                continue
            result.setdefault(line.line_no, {})[field] = row
    return result


def provenance(row: PurchaseCorrection) -> dict[str, Any]:
    return {
        "value": row.value,
        "source": "validated_correction",
        "evidence": row.evidence,
        "correction_id": str(row.id),
        "validated_by": row.validated_by,
        "valid_until": str(row.valid_until),
        "source_invoice_id": str(row.source_invoice_id),
    }


async def learned_accounts(
    session: AsyncSession,
    *,
    vendor_id: uuid.UUID | None,
    buyer: str | None,
    currency: str | None,
    invoice_date: date | None,
    lines: Sequence[InvoiceLine],
) -> dict[int, tuple[str, list[str]]]:
    """Reuse unanimous manual GL coding from at least two earlier approved bills."""
    if not vendor_id or not buyer or not currency or not invoice_date or not lines:
        return {}
    rows = await session.execute(
        select(
            Invoice.id, Invoice.bill_to_name, InvoiceLine.description, InvoiceLine.gl_account_code
        )
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(
            Invoice.vendor_id == vendor_id,
            Invoice.currency == currency,
            Invoice.status.in_((InvoiceStatus.APPROVED, InvoiceStatus.EXPORTED)),
            Invoice.hold_kind.is_(None),
            or_(Invoice.document_type.is_(None), Invoice.document_type != DocumentType.CREDIT_NOTE),
            Invoice.invoice_date >= date.fromordinal(max(1, invoice_date.toordinal() - 365)),
            Invoice.invoice_date < invoice_date,
            InvoiceLine.gl_source == "person",
        )
    )
    buyer_key = scope_key(buyer)
    descriptions = {line.line_no: scope_key(line.description) for line in lines}
    relevant = set(descriptions.values())
    history: dict[str, dict[str | None, set[str]]] = {}
    for bill_id, bill_to, description, account in rows:
        key = scope_key(description)
        if key in relevant and bill_to and scope_key(bill_to) == buyer_key:
            history.setdefault(key, {}).setdefault(account, set()).add(str(bill_id))
    learned = {}
    for number, key in descriptions.items():
        choices = history.get(key, {})
        if len(choices) != 1:
            continue
        account, bills = next(iter(choices.items()))
        if not account or len(bills) < 2:
            continue
        try:
            await validate_value(session, "gl_account", account, None)
        except CorrectionError:
            continue
        learned[number] = (account, sorted(bills))
    return learned


def record_use(session: AsyncSession, row: PurchaseCorrection, line: InvoiceLine) -> None:
    audit.record(
        session,
        action="correction.reused",
        entity_type="invoice",
        entity_id=line.invoice_id,
        data={
            "correction_id": str(row.id),
            "line_no": line.line_no,
            "field": row.field,
            "value": row.value,
        },
    )
