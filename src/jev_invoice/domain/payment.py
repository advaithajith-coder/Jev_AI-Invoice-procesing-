"""When to pay an invoice: the vendor's agreed terms, not what the invoice prints.

A vendor on net 30 is paid 30 days after the invoice date even if the invoice says "due
in 7 days"; if the invoice allows longer than the agreement, the later date is used.
An early-payment discount ("2/10 net 30": 2% off if paid within 10 days) is worth
taking, so it is worked out and shown next to the deadline.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class PaymentTerms:
    pay_by: date
    agreed_days: int
    printed_due: date | None
    """The due date printed on the invoice, if any."""
    asks_earlier: bool
    """The invoice asks for payment sooner than the agreed terms allow."""
    discount_pct: Decimal | None = None
    discount_days: int | None = None
    discount_by: date | None = None
    discount_amount: Decimal | None = None

    @property
    def label(self) -> str:
        """'2/10 net 30' or 'net 30'."""
        net = f"net {self.agreed_days}"
        if self.discount_pct is None or self.discount_days is None:
            return net
        return f"{self.discount_pct.normalize():f}/{self.discount_days} {net}"


def payment_terms(
    *,
    invoice_date: date,
    printed_due: date | None,
    total: Decimal | None,
    agreed_days: int,
    discount_pct: Decimal | None = None,
    discount_days: int | None = None,
) -> PaymentTerms:
    agreed_due = invoice_date + timedelta(days=agreed_days)
    pay_by = max(agreed_due, printed_due) if printed_due else agreed_due
    terms = PaymentTerms(
        pay_by=pay_by,
        agreed_days=agreed_days,
        printed_due=printed_due,
        asks_earlier=printed_due is not None and printed_due < agreed_due,
    )
    if discount_pct is None or discount_days is None:
        return terms
    return PaymentTerms(
        pay_by=terms.pay_by,
        agreed_days=agreed_days,
        printed_due=printed_due,
        asks_earlier=terms.asks_earlier,
        discount_pct=discount_pct,
        discount_days=discount_days,
        discount_by=invoice_date + timedelta(days=discount_days),
        discount_amount=(total * discount_pct / 100).quantize(_CENT) if total is not None else None,
    )
