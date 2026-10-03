from datetime import date
from decimal import Decimal

import pytest

from jev_invoice.domain.invoice import ExtractedInvoice, to_money


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1234.5, Decimal("1234.50")),
        ("1,234.50", Decimal("1234.50")),
        ("£1,234.50", Decimal("1234.50")),
        ("-164.50", Decimal("-164.50")),
        (Decimal("0.1") + Decimal("0.2"), Decimal("0.30")),
        (0, Decimal("0.00")),
        ("", None),
        ("n/a", None),
        (None, None),
        (True, None),
    ],
)
def test_to_money(raw: object, expected: Decimal | None) -> None:
    assert to_money(raw) == expected


def test_float_input_does_not_leak_binary_error() -> None:
    assert ExtractedInvoice(total=0.1 + 0.2).total == Decimal("0.30")


def test_dates_parse_iso_and_reject_garbage() -> None:
    invoice = ExtractedInvoice(invoice_date="2026-09-30", due_date="30/10/2026")
    assert invoice.invoice_date == date(2026, 9, 30)
    assert invoice.due_date is None  # not ISO: reported as missing, never guessed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("gbp", "GBP"), ("£", "GBP"), ("€", "EUR"), ("Pounds", None), ("", None), (None, None)],
)
def test_currency_normalisation(raw: str | None, expected: str | None) -> None:
    assert ExtractedInvoice(currency=raw).currency == expected


def test_discount_is_stored_as_positive_reduction() -> None:
    assert ExtractedInvoice(discount="-164.50").discount == Decimal("164.50")


def test_blank_strings_become_none() -> None:
    invoice = ExtractedInvoice(vendor_name="  ", po_reference="")
    assert invoice.vendor_name is None and invoice.po_reference is None


def test_raw_bank_account_never_serialised() -> None:
    invoice = ExtractedInvoice(
        vendor_name="Acme", bank_account="Routing 021000021 Account 4417926819"
    )

    assert invoice.bank_account is not None
    assert "bank_account" not in invoice.model_dump()
    assert "9268" not in invoice.model_dump_json()
    assert "9268" not in repr(invoice)


def test_additional_info_keeps_only_the_last_4_of_account_numbers() -> None:
    from jev_invoice.domain.invoice import ExtractedInvoice

    invoice = ExtractedInvoice.model_validate(
        {
            "remittance_details": "Remit to: Acme Supplies Inc, Routing 021000021 Account 4417926819",
            "notes": ["Thank you!", "Wire: IBAN DE89 3704 0044 0532 0130 00"],
            "late_fee_terms": "1.5% monthly finance charge on late payments",
            "payment_terms_text": "2% 10, Net 30",
        }
    )

    assert invoice.remittance_details == (
        "Remit to: Acme Supplies Inc, Routing 021000021 Account ••••6819"
    )
    assert "4417926819" not in invoice.remittance_details
    assert invoice.notes == ["Thank you!", "Wire: IBAN ••••3000"]
    assert invoice.late_fee_terms == "1.5% monthly finance charge on late payments"


def test_other_details_are_kept_as_label_and_value() -> None:
    from jev_invoice.domain.invoice import ExtractedInvoice

    invoice = ExtractedInvoice.model_validate(
        {
            "other_details": [
                {"label": "Order number", "value": "SO-55123"},
                {"label": "Customer account", "value": "NW-0042"},
                {"label": "Wire to", "value": "Account 4417926819"},
                {"label": "", "value": "dropped: no label"},
                {"label": "Empty", "value": "  "},
                "not a pair",
            ]
        }
    )

    assert [(d.label, d.value) for d in invoice.other_details] == [
        ("Order number", "SO-55123"),
        ("Customer account", "NW-0042"),
        ("Wire to", "Account ••••6819"),
    ]


def test_other_details_also_accepted_as_a_mapping() -> None:
    from jev_invoice.domain.invoice import ExtractedInvoice

    invoice = ExtractedInvoice.model_validate(
        {"other_details": {"Ship date": "09/28/2026", "Tracking": "1Z999AA10123456784"}}
    )

    assert [(d.label, d.value) for d in invoice.other_details] == [
        ("Ship date", "09/28/2026"),
        ("Tracking", "1Z999AA10123456784"),  # not a bank account: stays readable
    ]


def test_bare_account_number_under_a_payment_label_is_masked() -> None:
    from jev_invoice.domain.invoice import ExtractedInvoice

    invoice = ExtractedInvoice.model_validate(
        {"other_details": [{"label": "ACH payments to", "value": "021000021 4417926819"}]}
    )

    assert invoice.other_details[0].value == "021000021 ••••6819"
