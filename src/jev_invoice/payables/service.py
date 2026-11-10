"""What AP does with an invoice beyond approve or reject.

- Hold: stop export and payment until someone releases it, with a reason.
- Dispute: a hold raised with the vendor (wrong price, short delivery, ...), resolved
  with an outcome when the vendor answers.
- Comments: notes for colleagues, kept in the invoice's history.
- Reassign: hand an open review to a named person.
- Credit notes: a vendor credit applied to an invoice reduces what is owed on it, either
  an uploaded credit note or one entered from the vendor's credit number.
- Short-pay: pay less than billed, with a reason (for example damaged goods).
- Payments: what was paid, when, how, and any early-payment discount kept.

Every action is recorded in the audit trail against the person who did it.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.core import audit
from jev_invoice.core.errors import AppError, NotFoundError
from jev_invoice.db.models import Invoice, InvoiceCredit, Payment, ReviewTask
from jev_invoice.domain.enums import DocumentType, InvoiceStatus, ReviewStatus
from jev_invoice.domain.ledger import Balance, balance_of, withheld_after_credit
from jev_invoice.domain.payment import PaymentTerms, payment_terms
from jev_invoice.pipeline.service import invoice_query

_CENT = Decimal("0.01")

HOLD_KINDS = ("hold", "dispute")
DISPUTE_CATEGORIES: dict[str, str] = {
    "price": "Price is higher than agreed",
    "quantity": "Billed for more than was delivered",
    "not_received": "Goods or services not received",
    "damaged": "Goods damaged or faulty",
    "not_ordered": "Not ordered",
    "billed_twice": "Billed twice",
    "other": "Something else",
}
RELEASE_OUTCOMES: dict[str, str] = {
    "pay_as_billed": "Resolved: pay as billed",
    "credit_coming": "Resolved: vendor is sending a credit note",
    "short_pay": "Resolved: short-pay agreed",
    "other": "Released",
}
PAYMENT_METHODS: dict[str, str] = {
    "ach": "ACH transfer",
    "check": "Check",
    "wire": "Wire transfer",
    "card": "Company card",
}
PAYABLE_STATUSES = (InvoiceStatus.APPROVED, InvoiceStatus.EXPORTED)
OPEN_STATUSES = (InvoiceStatus.PENDING_REVIEW, *PAYABLE_STATUSES)


class PayablesError(AppError):
    status_code = 422
    code = "not_allowed"


def parse_amount(value: str | Decimal | None, *, label: str = "Amount") -> Decimal:
    try:
        amount = Decimal(str(value or "").replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise PayablesError(f"{label}: '{value}' is not an amount.") from exc
    if not amount.is_finite():
        raise PayablesError(f"{label}: '{value}' is not an amount.")
    return amount.quantize(_CENT)


def is_credit_note(invoice: Invoice) -> bool:
    return invoice.document_type == DocumentType.CREDIT_NOTE


def not_credit_note() -> ColumnElement[bool]:
    """SQL filter: invoices that are not credit notes (credit notes are never paid)."""
    return or_(Invoice.document_type.is_(None), Invoice.document_type != DocumentType.CREDIT_NOTE)


def credit_note_amount(invoice: Invoice) -> Decimal:
    """Credit notes may print their total as negative; the credit is its size."""
    amount = invoice.amount_due if invoice.amount_due is not None else invoice.total
    return abs(amount or Decimal(0))


def invoice_balance(invoice: Invoice) -> Balance:
    """Needs `payments` and `credits` loaded (invoice_query loads them)."""
    if is_credit_note(invoice):
        zero = Decimal(0)
        return Balance(zero, zero, zero, zero, zero)
    return balance_of(
        total=invoice.total,
        amount_due=invoice.amount_due,
        credits=[credit.amount for credit in invoice.credits],
        withheld=invoice.withheld_amount,
        payments=[(payment.amount, payment.discount_taken) for payment in invoice.payments],
    )


def invoice_terms(invoice: Invoice) -> PaymentTerms | None:
    """When and how much to pay: agreed terms, discount on what is still owed."""
    if invoice.invoice_date is None:
        return None
    vendor = invoice.vendor
    return payment_terms(
        invoice_date=invoice.invoice_date,
        printed_due=invoice.due_date,
        # The early-payment discount applies to what is still owed.
        total=invoice.amount_due if invoice.amount_due is not None else invoice.total,
        agreed_days=vendor.payment_terms_days if vendor else 30,
        discount_pct=vendor.early_pay_discount_pct if vendor else None,
        discount_days=vendor.early_pay_days if vendor else None,
    )


def discount_available(invoice: Invoice, on: date) -> Decimal:
    """The early-payment discount that may still be kept when paying on *on*."""
    terms = invoice_terms(invoice)
    if (
        terms is None
        or terms.discount_by is None
        or terms.discount_amount is None
        or on > terms.discount_by
        or any(payment.discount_taken for payment in invoice.payments)
    ):
        return Decimal(0)
    return min(terms.discount_amount, invoice_balance(invoice).balance)


@dataclass(frozen=True)
class PaymentPlan:
    """Pre-filled values for the payment form."""

    amount: Decimal
    discount: Decimal


def payment_plan(invoice: Invoice, on: date) -> PaymentPlan:
    balance = invoice_balance(invoice).balance
    discount = discount_available(invoice, on)
    return PaymentPlan(amount=balance - discount, discount=discount)


class PayablesService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, invoice_id: uuid.UUID) -> Invoice:
        invoice = await self._session.scalar(invoice_query().where(Invoice.id == invoice_id))
        if invoice is None:
            raise NotFoundError("Invoice not found", details={"invoice_id": str(invoice_id)})
        return invoice

    # ── Holds and disputes ────────────────────────────────────────────────────

    async def hold(
        self,
        invoice_id: uuid.UUID,
        *,
        kind: str,
        reason: str,
        category: str | None = None,
        actor: str,
        commit: bool = True,
    ) -> Invoice:
        invoice = await self.get(invoice_id)
        reason = reason.strip()
        if kind not in HOLD_KINDS:
            raise PayablesError("Choose a hold or a dispute.")
        if invoice.status not in (InvoiceStatus.EXTRACTED, *OPEN_STATUSES):
            raise PayablesError(f"A {invoice.status} invoice cannot be put on hold.")
        if invoice.hold_kind:
            raise PayablesError("This invoice is already on hold. Release it first.")
        if kind == "dispute" and category not in DISPUTE_CATEGORIES:
            raise PayablesError("Choose what the dispute is about.")
        if not reason:
            raise PayablesError("Say why, so whoever picks this up knows what is being waited for.")
        if invoice.status in PAYABLE_STATUSES and invoice_balance(invoice).state == "paid":
            raise PayablesError("This invoice is already paid.")
        invoice.hold_kind = kind
        invoice.hold_category = category if kind == "dispute" else None
        invoice.hold_reason = reason[:2000]
        invoice.held_by = actor
        invoice.held_at = datetime.now(UTC)
        audit.record(
            self._session,
            action="invoice.disputed" if kind == "dispute" else "invoice.held",
            entity_type="invoice",
            entity_id=invoice.id,
            actor=actor,
            data={
                "note": invoice.hold_reason,
                **({"category": category} if kind == "dispute" else {}),
            },
        )
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return invoice

    async def release(
        self, invoice_id: uuid.UUID, *, outcome: str, note: str, actor: str
    ) -> Invoice:
        invoice = await self.get(invoice_id)
        if not invoice.hold_kind:
            raise PayablesError("This invoice is not on hold.")
        if outcome not in RELEASE_OUTCOMES:
            raise PayablesError("Choose how it was resolved.")
        note = note.strip()
        if invoice.hold_kind == "dispute" and not note:
            raise PayablesError("Say what the vendor agreed, for the record.")
        disputed = invoice.hold_kind == "dispute"
        audit.record(
            self._session,
            action="invoice.dispute_resolved" if disputed else "invoice.released",
            entity_type="invoice",
            entity_id=invoice.id,
            actor=actor,
            data={
                "outcome": outcome,
                "outcome_label": RELEASE_OUTCOMES[outcome],
                "note": note[:2000] or None,
                "held_for": invoice.hold_reason,
            },
        )
        invoice.hold_kind = None
        invoice.hold_category = None
        invoice.hold_reason = None
        invoice.held_by = None
        invoice.held_at = None
        await self._session.commit()
        return invoice

    # ── Comments and assignment ───────────────────────────────────────────────

    async def comment(self, invoice_id: uuid.UUID, *, text: str, actor: str) -> None:
        invoice = await self.get(invoice_id)
        text = text.strip()
        if not text:
            raise PayablesError("Write a comment first.")
        audit.record(
            self._session,
            action="invoice.commented",
            entity_type="invoice",
            entity_id=invoice.id,
            actor=actor,
            data={"note": text[:2000]},
        )
        await self._session.commit()

    async def assign(
        self, invoice_id: uuid.UUID, *, assignee: str, note: str, actor: str
    ) -> ReviewTask:
        task = await self._session.scalar(
            select(ReviewTask).where(
                ReviewTask.invoice_id == invoice_id, ReviewTask.status == ReviewStatus.OPEN
            )
        )
        if task is None:
            raise PayablesError("This invoice has no open review to assign.")
        assignee = assignee.strip()[:100]
        previous = task.assigned_to
        task.assigned_to = assignee or None
        audit.record(
            self._session,
            action="review.assigned" if assignee else "review.unassigned",
            entity_type="invoice",
            entity_id=invoice_id,
            actor=actor,
            data={"from": previous, "to": assignee or None, "note": note.strip()[:2000] or None},
        )
        await self._session.commit()
        return task

    # ── Short-pay ─────────────────────────────────────────────────────────────

    async def short_pay(
        self, invoice_id: uuid.UUID, *, amount: Decimal, reason: str, actor: str
    ) -> Invoice:
        """Withhold *amount* of the bill (0 clears an earlier short-pay)."""
        invoice = await self.get(invoice_id)
        reason = reason.strip()
        if is_credit_note(invoice) or invoice.status not in OPEN_STATUSES:
            raise PayablesError("Only invoices in review or approved can be short-paid.")
        if amount < 0:
            raise PayablesError("The amount to withhold cannot be negative.")
        balance = invoice_balance(invoice)
        headroom = balance.balance + balance.withheld
        if amount > headroom:
            raise PayablesError(
                f"You can withhold at most {headroom:,.2f}: that is all that is still unpaid."
            )
        if amount and not reason:
            raise PayablesError("Say why you are paying less, for the vendor and the auditors.")
        previous = invoice.withheld_amount
        invoice.withheld_amount = amount or None
        invoice.withheld_reason = reason[:2000] if amount else None
        audit.record(
            self._session,
            action="invoice.short_paid" if amount else "invoice.short_pay_cleared",
            entity_type="invoice",
            entity_id=invoice.id,
            actor=actor,
            data={
                "amount": str(amount),
                "previous": str(previous) if previous else None,
                "note": reason or None,
            },
        )
        await self._session.commit()
        return invoice

    # ── Credit notes ──────────────────────────────────────────────────────────

    async def unapplied_credit_notes(self, invoice: Invoice) -> list[Invoice]:
        """Uploaded credit notes from this invoice's vendor not yet applied anywhere."""
        if invoice.vendor_id is None or is_credit_note(invoice):
            return []
        applied = select(InvoiceCredit.credit_note_id).where(
            InvoiceCredit.credit_note_id.is_not(None)
        )
        return list(
            await self._session.scalars(
                select(Invoice).where(
                    Invoice.vendor_id == invoice.vendor_id,
                    Invoice.document_type == DocumentType.CREDIT_NOTE,
                    Invoice.status == InvoiceStatus.PENDING_REVIEW,
                    Invoice.id.not_in(applied),
                )
            )
        )

    async def credit_targets(self, credit_note: Invoice) -> list[tuple[Invoice, Balance]]:
        """Open invoices from the credit note's vendor it could be applied to."""
        query = invoice_query().where(Invoice.status.in_(OPEN_STATUSES), not_credit_note())
        if credit_note.vendor_id is not None:
            query = query.where(Invoice.vendor_id == credit_note.vendor_id)
        elif credit_note.vendor_name:
            query = query.where(Invoice.vendor_name == credit_note.vendor_name)
        else:
            return []
        targets = []
        for invoice in await self._session.scalars(query.order_by(Invoice.invoice_date)):
            balance = invoice_balance(invoice)
            if balance.balance + balance.withheld > 0:
                targets.append((invoice, balance))
        return targets

    async def apply_credit(
        self,
        invoice_id: uuid.UUID,
        *,
        actor: str,
        credit_note_id: uuid.UUID | None = None,
        number: str = "",
        amount: Decimal | None = None,
        reason: str = "",
        commit: bool = True,
    ) -> InvoiceCredit:
        """Apply an uploaded credit note (by id) or a credit entered by hand (number, amount)."""
        invoice = await self.get(invoice_id)
        if is_credit_note(invoice):
            raise PayablesError("A credit note cannot be applied to another credit note.")
        if invoice.status not in OPEN_STATUSES:
            raise PayablesError(f"Credits apply to open invoices, not {invoice.status} ones.")

        credit_note: Invoice | None = None
        if credit_note_id is not None:
            credit_note = await self.get(credit_note_id)
            if not is_credit_note(credit_note):
                raise PayablesError("That document is not a credit note.")
            if await self._session.scalar(
                select(InvoiceCredit.id).where(InvoiceCredit.credit_note_id == credit_note.id)
            ):
                raise PayablesError("That credit note has already been applied.")
            if credit_note.vendor_id and credit_note.vendor_id != invoice.vendor_id:
                raise PayablesError("The credit note is from a different vendor.")
            amount = credit_note_amount(credit_note)
            number = credit_note.invoice_number or credit_note.original_filename
        number = number.strip()[:100]
        if not number:
            raise PayablesError("Enter the vendor's credit note number.")
        if amount is None or amount <= 0:
            raise PayablesError("The credit must be more than zero.")
        balance = invoice_balance(invoice)
        headroom = balance.balance + balance.withheld
        if amount > headroom:
            raise PayablesError(
                f"The credit ({amount:,.2f}) is more than is still owed on this invoice"
                f" ({headroom:,.2f}). Apply it to another invoice from this vendor."
            )

        credit = InvoiceCredit(
            invoice_id=invoice.id,
            credit_note_id=credit_note.id if credit_note else None,
            number=number,
            amount=amount,
            reason=reason.strip()[:2000] or None,
            recorded_by=actor,
        )
        self._session.add(credit)
        invoice.withheld_amount = withheld_after_credit(invoice.withheld_amount, amount)
        if invoice.withheld_amount is None:
            invoice.withheld_reason = None
        audit.record(
            self._session,
            action="invoice.credit_applied",
            entity_type="invoice",
            entity_id=invoice.id,
            actor=actor,
            data={"number": number, "amount": str(amount), "note": credit.reason},
        )
        if credit_note is not None:
            await self._close_credit_note(credit_note, invoice, actor)
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return credit

    async def _close_credit_note(self, credit_note: Invoice, target: Invoice, actor: str) -> None:
        """An applied credit note is done: its review closes and it counts as approved."""
        label = target.invoice_number or str(target.id)
        task = await self._session.scalar(
            select(ReviewTask).where(
                ReviewTask.invoice_id == credit_note.id, ReviewTask.status == ReviewStatus.OPEN
            )
        )
        if task is not None:
            task.status = ReviewStatus.APPROVED
            task.resolved_by = actor
            task.resolved_at = datetime.now(UTC)
            task.resolution_note = f"Applied to invoice {label}"
        credit_note.status = InvoiceStatus.APPROVED
        audit.record(
            self._session,
            action="credit_note.applied",
            entity_type="invoice",
            entity_id=credit_note.id,
            actor=actor,
            data={"invoice_id": str(target.id), "invoice_number": label},
        )

    # ── Payments ──────────────────────────────────────────────────────────────

    async def record_payment(
        self,
        invoice_id: uuid.UUID,
        *,
        paid_on: date,
        amount: Decimal,
        take_discount: bool,
        method: str,
        reference: str,
        actor: str,
    ) -> Payment:
        invoice = await self.get(invoice_id)
        if is_credit_note(invoice):
            raise PayablesError("Credit notes are applied to invoices, not paid.")
        if invoice.status not in PAYABLE_STATUSES:
            raise PayablesError("Only approved invoices can be paid.")
        if invoice.hold_kind:
            raise PayablesError(
                "This invoice is on hold"
                f"{' (disputed)' if invoice.hold_kind == 'dispute' else ''}. Release it first."
            )
        if method not in PAYMENT_METHODS:
            raise PayablesError("Choose how it was paid.")
        if paid_on > datetime.now(UTC).date():
            raise PayablesError("Record payments once they are made, not in advance.")
        if amount <= 0:
            raise PayablesError("The payment must be more than zero.")
        discount = discount_available(invoice, paid_on) if take_discount else Decimal(0)
        if take_discount and not discount:
            raise PayablesError(
                "There is no early-payment discount to keep on that date (it ended or was taken)."
            )
        balance = invoice_balance(invoice).balance
        if amount + discount > balance:
            raise PayablesError(
                f"That is more than is still owed ({balance:,.2f}"
                f"{f', or {balance - discount:,.2f} after the discount' if discount else ''})."
            )
        payment = Payment(
            invoice_id=invoice.id,
            paid_on=paid_on,
            amount=amount,
            discount_taken=discount,
            method=method,
            reference=reference.strip()[:100] or None,
            recorded_by=actor,
        )
        self._session.add(payment)
        audit.record(
            self._session,
            action="invoice.payment_recorded",
            entity_type="invoice",
            entity_id=invoice.id,
            actor=actor,
            data={
                "amount": str(amount),
                "discount_taken": str(discount),
                "paid_on": paid_on.isoformat(),
                "method": method,
                "reference": payment.reference,
            },
        )
        await self._session.commit()
        return payment
