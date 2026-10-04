"""What is still owed on an invoice, after everything that settles part of it.

    billed     the invoice's balance due (its total less any deposit already paid)
  - credited   vendor credit notes applied to it
  - withheld   a short-pay: part of the bill the company refuses to pay, with a reason
  - paid       payments recorded against it
  - discount   early-payment discounts kept when paying inside the window
  = balance    what is still to pay

A credit note that arrives for a short-paid invoice settles the withheld part first
(the vendor has agreed to the reduction), so the balance does not drop twice.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

ZERO = Decimal("0.00")

PaymentState = Literal["unpaid", "part_paid", "paid"]


@dataclass(frozen=True)
class Balance:
    billed: Decimal
    credited: Decimal
    withheld: Decimal
    paid: Decimal
    discount: Decimal

    @property
    def settled(self) -> Decimal:
        return self.credited + self.withheld + self.paid + self.discount

    @property
    def balance(self) -> Decimal:
        return max(self.billed - self.settled, ZERO)

    @property
    def state(self) -> PaymentState:
        if self.billed > 0 and self.balance == 0:
            return "paid"
        if self.paid > 0 or self.discount > 0:
            return "part_paid"
        return "unpaid"


def balance_of(
    *,
    total: Decimal | None,
    amount_due: Decimal | None,
    credits: list[Decimal],
    withheld: Decimal | None,
    payments: list[tuple[Decimal, Decimal]],
) -> Balance:
    """*payments* are (amount, discount_taken) pairs."""
    billed = amount_due if amount_due is not None else (total or ZERO)
    return Balance(
        billed=billed,
        credited=sum(credits, ZERO),
        withheld=withheld or ZERO,
        paid=sum((amount for amount, _ in payments), ZERO),
        discount=sum((discount for _, discount in payments), ZERO),
    )


def withheld_after_credit(withheld: Decimal | None, credit: Decimal) -> Decimal | None:
    """A credit settles a short-pay first: what remains withheld after applying *credit*."""
    if not withheld:
        return withheld
    remaining = withheld - credit
    return remaining if remaining > 0 else None
