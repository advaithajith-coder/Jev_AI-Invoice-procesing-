from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from jev_invoice.domain.checks import check_extraction
from jev_invoice.domain.enums import ReasonCode
from jev_invoice.domain.invoice import ExtractedInvoice

TODAY = date(2026, 10, 2)
LIMIT = Decimal("5000.00")


def _invoice(**overrides: Any) -> ExtractedInvoice:
    fields: dict[str, Any] = {
        "vendor_name": "Nexus Software",
        "invoice_number": "NSS-1",
        "invoice_date": "2026-09-30",
        "due_date": "2026-10-30",
        "subtotal": "3290.00",
        "discount": "164.50",
        "tax": "0",
        "total": "3125.50",
        "line_items": [
            {
                "description": "CRM licence",
                "quantity": 5,
                "unit_price": "200.00",
                "amount": "1000.00",
            },
            {"description": "Analytics", "quantity": 5, "unit_price": "150.00", "amount": "750.00"},
            {"description": "Modules", "quantity": 7, "unit_price": "220.00", "amount": "1540.00"},
        ],
    }
    fields.update(overrides)
    return ExtractedInvoice.model_validate(fields)


def _codes(invoice: ExtractedInvoice, **kwargs: Any) -> list[ReasonCode]:
    findings = check_extraction(
        invoice,
        today=kwargs.get("today", TODAY),
        tolerance=Decimal("0.02"),
        approval_limit=kwargs.get("limit", LIMIT),
    )
    return [finding.code for finding in findings]


def test_discounted_invoice_that_adds_up_passes() -> None:
    # 3290.00 - 164.50 discount + 0 tax = 3125.50
    assert _codes(_invoice()) == []


def test_missing_required_fields_reported_once_with_names() -> None:
    findings = check_extraction(
        _invoice(vendor_name=None, total=None),
        today=TODAY,
        tolerance=Decimal("0.02"),
        approval_limit=LIMIT,
    )
    missing = [f for f in findings if f.code == ReasonCode.MISSING_REQUIRED_FIELD]
    assert len(missing) == 1
    assert "vendor_name" in missing[0].message and "total" in missing[0].message


def test_total_mismatch() -> None:
    assert ReasonCode.TOTALS_MISMATCH in _codes(_invoice(total="3290.00"))


def test_lines_not_matching_subtotal() -> None:
    assert ReasonCode.TOTALS_MISMATCH in _codes(_invoice(subtotal="3300.00", total="3135.50"))


def test_line_quantity_times_price_mismatch() -> None:
    lines = [{"description": "x", "quantity": 2, "unit_price": "10.00", "amount": "25.00"}]
    invoice = _invoice(line_items=lines, subtotal="25.00", discount=None, total="25.00")
    assert _codes(invoice) == [ReasonCode.TOTALS_MISMATCH]


def test_rounding_within_tolerance_passes() -> None:
    assert _codes(_invoice(total="3125.52")) == []
    assert ReasonCode.TOTALS_MISMATCH in _codes(_invoice(total="3125.53"))


def test_tax_is_added() -> None:
    invoice = _invoice(tax="625.10", total="3750.60")
    assert _codes(invoice) == []


def test_invoice_without_lines_or_subtotal_skips_arithmetic() -> None:
    assert _codes(_invoice(line_items=[], subtotal=None, discount=None, total="99.00")) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"invoice_date": "2026-10-03"}, []),  # one day of grace for time zones
        ({"invoice_date": "2026-10-04", "due_date": "2026-11-04"}, [ReasonCode.INVALID_DATES]),
        ({"due_date": "2026-09-01"}, [ReasonCode.INVALID_DATES]),
    ],
)
def test_dates(overrides: dict[str, Any], expected: list[ReasonCode]) -> None:
    assert _codes(_invoice(**overrides)) == expected


def test_approval_limit_is_strictly_greater() -> None:
    assert _codes(_invoice(), limit=Decimal("3125.50")) == []
    assert _codes(_invoice(), limit=Decimal("3125.49")) == [ReasonCode.OVER_APPROVAL_LIMIT]


@pytest.mark.parametrize(
    ("currency", "expected"),
    [
        ("USD", []),
        (None, []),  # nothing printed: assumed USD, still checked against vendor and PO
        ("EUR", [ReasonCode.FOREIGN_CURRENCY]),
        ("CAD", [ReasonCode.FOREIGN_CURRENCY]),
    ],
)
def test_only_the_base_currency_passes(currency: str | None, expected: list[ReasonCode]) -> None:
    assert _codes(_invoice(currency=currency)) == expected


def test_another_currency_with_a_rate_is_checked_in_dollars() -> None:
    inr = _invoice(currency="INR", subtotal="301050", discount="0", total="301050", line_items=[])

    def codes(rate: str) -> list[ReasonCode]:
        findings = check_extraction(
            inr,
            today=TODAY,
            tolerance=Decimal("0.02"),
            approval_limit=LIMIT,
            fx_rate=Decimal(rate),
        )
        return [finding.code for finding in findings]

    assert codes("0.01038") == []  # USD 3,124.90
    assert codes("0.02") == [ReasonCode.OVER_APPROVAL_LIMIT]  # USD 6,021.00


def test_foreign_currency_goes_to_a_person() -> None:
    from jev_invoice.decision.policy import decide
    from jev_invoice.domain.enums import DecisionAction
    from tests.fakes import clean_jev_answers

    findings = check_extraction(
        _invoice(currency="EUR"), today=TODAY, tolerance=Decimal("0.02"), approval_limit=LIMIT
    )
    outcome = decide(findings=findings, jev=clean_jev_answers(), jev_status="ok")

    assert outcome.action == DecisionAction.HOLD_REVIEW
    assert outcome.reason_codes == [ReasonCode.FOREIGN_CURRENCY]


def test_very_old_invoice_is_flagged() -> None:
    assert _codes(_invoice(invoice_date="2026-03-01", due_date="2026-03-31")) == [
        ReasonCode.STALE_INVOICE
    ]
    assert _codes(_invoice(invoice_date="2026-06-01", due_date="2026-07-01")) == []


def test_shipping_and_amounts_already_paid_add_up() -> None:
    # 3,290.00 - 164.50 discount + 35.00 shipping + 0 tax = 3,160.50; 500 deposit paid.
    invoice = _invoice(shipping="35.00", total="3160.50", amount_paid="-500", amount_due="2660.50")

    assert _codes(invoice) == []
    assert invoice.amount_paid == Decimal("500.00")  # stored as a positive amount


def test_balance_due_must_match_total_minus_paid() -> None:
    invoice = _invoice(amount_paid="500", amount_due="2000.00")

    assert _codes(invoice) == [ReasonCode.TOTALS_MISMATCH]


@pytest.mark.parametrize(
    ("bill_to", "expected"),
    [
        ("Northwind Analytics Inc", []),
        ("NORTHWIND ANALYTICS", []),  # without the legal suffix
        (None, []),  # nothing printed
        ("Northwind Holdings LLC", [ReasonCode.WRONG_BILL_TO]),
    ],
)
def test_invoice_must_be_addressed_to_us(bill_to: str | None, expected: list[ReasonCode]) -> None:
    findings = check_extraction(
        _invoice(bill_to_name=bill_to),
        today=TODAY,
        tolerance=Decimal("0.02"),
        approval_limit=LIMIT,
        company_name="Northwind Analytics Inc",
    )
    assert [finding.code for finding in findings] == expected
