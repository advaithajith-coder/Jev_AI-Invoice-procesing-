"""Agreed payment terms and early-payment discounts."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jev_invoice.domain.payment import payment_terms
from jev_invoice.masterdata.vendors import VendorFields

INVOICED = date(2026, 9, 30)


def test_pay_on_agreed_terms_even_if_the_invoice_asks_sooner() -> None:
    terms = payment_terms(
        invoice_date=INVOICED, printed_due=date(2026, 10, 7), total=Decimal("100"), agreed_days=30
    )

    assert terms.pay_by == date(2026, 10, 30)
    assert terms.asks_earlier is True
    assert terms.label == "net 30"


def test_a_later_printed_due_date_is_kept() -> None:
    terms = payment_terms(
        invoice_date=INVOICED, printed_due=date(2026, 11, 30), total=None, agreed_days=30
    )

    assert terms.pay_by == date(2026, 11, 30)
    assert terms.asks_earlier is False


def test_early_payment_discount_is_worked_out() -> None:
    terms = payment_terms(
        invoice_date=INVOICED,
        printed_due=None,
        total=Decimal("1074.00"),
        agreed_days=30,
        discount_pct=Decimal("2"),
        discount_days=10,
    )

    assert terms.label == "2/10 net 30"
    assert terms.discount_by == date(2026, 10, 10)
    assert terms.discount_amount == Decimal("21.48")


@pytest.mark.parametrize(
    ("discount", "message"),
    [
        ({"early_pay_discount_pct": "2"}, "both a percentage and a number of days"),
        ({"early_pay_days": 10}, "both a percentage and a number of days"),
        ({"early_pay_discount_pct": "2", "early_pay_days": 30}, "shorter than the payment terms"),
    ],
)
def test_discount_terms_must_be_complete_and_shorter(
    discount: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        VendorFields(name="X", default_gl_account_code="6000", payment_terms_days=30, **discount)
