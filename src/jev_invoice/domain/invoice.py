"""Typed invoice data as it moves through the pipeline.

Money is Decimal, dates are `date`. The extraction LLM returns loosely formatted JSON;
the validators here normalise it, and anything that cannot be read becomes None rather
than a guess. Missing values are reported by the checks, not silently filled in.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jev_invoice.domain.enums import ReasonCode
from jev_invoice.domain.vendors import mask_account_numbers

_CENT = Decimal("0.01")
_NOT_A_NUMBER = re.compile(r"[^\d.\-]")
_CURRENCY_SYMBOLS = {"£": "GBP", "€": "EUR", "$": "USD", "¥": "JPY"}


def to_money(value: Any) -> Decimal | None:
    """Parse 1234.5, "1,234.50", "£1,234.50" or "-164.50" into Decimal('1234.50')."""
    number = to_decimal(value)
    return None if number is None else number.quantize(_CENT)


def to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = _NOT_A_NUMBER.sub("", value.replace(",", ""))
        if cleaned in ("", "-", ".", "-."):
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


class ExtractedLine(BaseModel):
    description: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None

    _num = field_validator("quantity", "unit_price", mode="before")(to_decimal)
    _money = field_validator("amount", mode="before")(to_money)


class OtherDetail(BaseModel):
    """Any other labelled information printed on the invoice, e.g. "Order number: SO-55123"."""

    label: str
    value: str


MAX_OTHER_DETAILS = 30
_PAYMENT_LABEL = re.compile(r"bank|wire|remit|pay|account|acct|iban|routing|ach", re.I)


class ExtractedInvoice(BaseModel):
    """Fields read from the document. None means "not found", never "zero"."""

    model_config = ConfigDict(str_strip_whitespace=True)

    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    po_reference: str | None = None
    # What the invoice is for, as stated on it (a project, period or service), else a
    # short summary of the lines.
    description: str | None = None
    description_printed: bool | None = None
    """True when the description is on the document; False when the model summarised it."""
    # Who the invoice is addressed to: should be our own company.
    bill_to_name: str | None = None
    # The seller's tax ID as printed (EIN in the US); compared with the vendor record.
    vendor_tax_id: str | None = None
    subtotal: Decimal | None = None
    discount: Decimal | None = None
    shipping: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    """The invoice total, before subtracting anything already paid."""
    amount_paid: Decimal | None = None
    """Deposits, prepayments or credits already applied, as a positive number."""
    amount_due: Decimal | None = None
    """The balance still to pay: total minus amount_paid."""
    line_items: list[ExtractedLine] = Field(default_factory=list)
    # Additional information, as printed. Long account numbers are masked to the last 4.
    payment_terms_text: str | None = None
    late_fee_terms: str | None = None
    remittance_details: str | None = None
    notes: list[str] = Field(default_factory=list)
    # Anything else labelled on the invoice that no field above covers (order number,
    # customer account, contract, ship date, tracking number...). Shown, never checked.
    other_details: list[OtherDetail] = Field(default_factory=list)
    # Raw payee account as printed. Transient: excluded from dumps, logs, and the API.
    # The pipeline turns it into last-4 + HMAC fingerprint and then drops it.
    bank_account: str | None = Field(default=None, exclude=True, repr=False)

    _money = field_validator(
        "subtotal",
        "discount",
        "shipping",
        "tax",
        "total",
        "amount_paid",
        "amount_due",
        mode="before",
    )(to_money)

    @field_validator("invoice_date", "due_date", mode="before")
    @classmethod
    def _iso_date_or_none(cls, value: Any) -> date | None:
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError:
                return None
        return None

    @field_validator("currency", mode="before")
    @classmethod
    def _iso_currency_or_none(cls, value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text in _CURRENCY_SYMBOLS:
            return _CURRENCY_SYMBOLS[text]
        code = text.upper()
        return code if re.fullmatch(r"[A-Z]{3}", code) else None

    @field_validator("discount", "amount_paid", mode="after")
    @classmethod
    def _discount_is_positive(cls, value: Decimal | None) -> Decimal | None:
        # Invoices print discounts as "-164.50" or "164.50"; store the size of the reduction.
        return None if value is None else abs(value)

    @field_validator(
        "vendor_name",
        "invoice_number",
        "po_reference",
        "bank_account",
        "description",
        "bill_to_name",
        "vendor_tax_id",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("payment_terms_text", "late_fee_terms", "remittance_details", mode="before")
    @classmethod
    def _masked_text(cls, value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return mask_account_numbers(value.strip())

    @field_validator("notes", mode="before")
    @classmethod
    def _masked_notes(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [mask_account_numbers(str(note).strip()) for note in value if str(note).strip()]

    @field_validator("other_details", mode="before")
    @classmethod
    def _masked_details(cls, value: Any) -> list[dict[str, str]]:
        """Accept [{"label", "value"}] or {"label": "value"}; drop blanks; mask accounts."""
        if isinstance(value, dict):
            value = [{"label": key, "value": item} for key, item in value.items()]
        if not isinstance(value, list):
            return []
        details: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            text = str(item.get("value") or "").strip()
            if label and text:
                # Long digit runs are masked only where the label is about paying: a tracking
                # or order number stays readable, an account number never does.
                payment = _PAYMENT_LABEL.search(label) is not None
                value_text = mask_account_numbers(text, long_digits=payment)
                details.append({"label": label[:100], "value": value_text[:500]})
        return details[:MAX_OTHER_DETAILS]


class CheckFinding(BaseModel):
    """One problem found by an exact check, with a human-readable explanation."""

    code: ReasonCode
    message: str
