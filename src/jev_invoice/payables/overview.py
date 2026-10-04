"""The payables invoice list: every approved invoice with what is still owed on it.

Every approved invoice with something still owed is an *item*. Each item has:

- its balance (after deposits, credits, short-pays and payments: see domain/ledger.py),
- its pay-by date on the vendor's agreed terms, and how many days late it is,
- a planned payment date: the last day of its early-payment discount while one is open
  (keeping the discount), otherwise its pay-by date; late items are paid now.

The list is split into tabs (all open, late, due in 7 days, discount open, on hold,
paid). Held and disputed invoices are still owed, but are not planned for payment
until released.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.db.models import Invoice
from jev_invoice.domain.ledger import Balance
from jev_invoice.domain.payment import PaymentTerms
from jev_invoice.payables.service import (
    PAYABLE_STATUSES,
    discount_available,
    invoice_balance,
    invoice_terms,
    not_credit_note,
)
from jev_invoice.pipeline.service import invoice_query

ZERO = Decimal("0.00")

TABS: dict[str, str] = {
    "open": "All open",
    "late": "Late",
    "week": "Due in 7 days",
    "discount": "Discount open",
    "held": "On hold",
    "paid": "Paid",
}
PAID_LIMIT = 100


def today() -> date:
    return datetime.now(UTC).date()


def vendor_name(invoice: Invoice) -> str:
    return invoice.vendor.name if invoice.vendor else (invoice.vendor_name or "Unknown vendor")


# ── Items ─────────────────────────────────────────────────────────────────────


@dataclass
class Item:
    invoice: Invoice
    vendor: str
    balance: Balance
    terms: PaymentTerms | None
    days_late: int
    """Days past the pay-by date (0 when not yet due)."""
    pay_on: date
    """Planned payment date: discount deadline while one is open, else pay-by; today if late."""
    discount: Decimal
    """Early-payment discount kept by paying on `pay_on`."""

    @property
    def owed(self) -> Decimal:
        return self.balance.balance

    @property
    def to_pay(self) -> Decimal:
        return self.owed - self.discount

    # In the company's currency, so invoices in different currencies add up.
    @property
    def converted(self) -> bool:
        return self.invoice.fx_rate is not None

    @property
    def owed_base(self) -> Decimal:
        return self.invoice.in_base(self.owed)

    @property
    def discount_base(self) -> Decimal:
        return self.invoice.in_base(self.discount)

    @property
    def to_pay_base(self) -> Decimal:
        return self.invoice.in_base(self.to_pay)

    @property
    def paid_base(self) -> Decimal:
        return self.invoice.in_base(self.balance.paid)

    @property
    def paid_discount_base(self) -> Decimal:
        return self.invoice.in_base(self.balance.discount)

    @property
    def held(self) -> bool:
        return bool(self.invoice.hold_kind)

    @property
    def late(self) -> bool:
        return self.days_late > 0

    @property
    def days_until(self) -> int:
        return (self.pay_on - today()).days

    @property
    def last_paid(self) -> date | None:
        return max((payment.paid_on for payment in self.invoice.payments), default=None)


def make_item(invoice: Invoice, on: date) -> Item:
    terms = invoice_terms(invoice)
    balance = invoice_balance(invoice)
    discount = ZERO
    days_late = 0
    if terms is None:
        pay_on = on
    elif terms.discount_by is not None and terms.discount_by >= on:
        discount = discount_available(invoice, terms.discount_by)
        pay_on = terms.discount_by if discount else terms.pay_by
    else:
        pay_on = terms.pay_by
    if terms is not None and terms.pay_by < on and not discount:
        days_late = (on - terms.pay_by).days
        pay_on = on
    return Item(invoice, vendor_name(invoice), balance, terms, days_late, pay_on, discount)


# ── The list ──────────────────────────────────────────────────────────────────


@dataclass
class Overview:
    today: date
    items: list[Item]
    """Everything with a balance, held or not."""
    paid: list[Item]

    @property
    def held(self) -> list[Item]:
        return [item for item in self.items if item.held]

    @property
    def late(self) -> list[Item]:
        return [item for item in self.items if item.late and not item.held]

    @property
    def due_week(self) -> list[Item]:
        """Planned for payment in the next 7 days (late ones are under "late")."""
        return [
            item for item in self.items if not item.held and not item.late and item.days_until < 7
        ]

    @property
    def discounts(self) -> list[Item]:
        return [item for item in self.items if item.discount and not item.held]

    def tab(self, name: str) -> list[Item]:
        if name == "paid":
            return self.paid
        if name == "late":
            return sorted(self.late, key=lambda item: -item.days_late)
        if name == "week":
            return self.due_week
        if name == "discount":
            return sorted(self.discounts, key=lambda item: item.pay_on)
        if name == "held":
            return self.held
        return self.items

    def counts(self) -> dict[str, int]:
        return {name: len(self.tab(name)) for name in TABS}


async def overview(session: AsyncSession) -> Overview:
    on = today()
    items: list[Item] = []
    paid: list[Item] = []
    invoices = await session.scalars(
        invoice_query().where(Invoice.status.in_(PAYABLE_STATUSES), not_credit_note())
    )
    for invoice in invoices:
        item = make_item(invoice, on)
        if item.owed > 0:
            items.append(item)
        elif item.balance.state == "paid":
            paid.append(item)
    items.sort(key=lambda item: (item.pay_on, -item.days_late, item.vendor))
    paid.sort(key=lambda item: item.last_paid or date.min, reverse=True)
    return Overview(today=on, items=items, paid=paid[:PAID_LIMIT])
