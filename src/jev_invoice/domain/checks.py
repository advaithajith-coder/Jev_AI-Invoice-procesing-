"""Exact checks on an extracted invoice. Pure functions: no I/O, no model calls.

These are the questions with one right answer (does it add up, are the dates sane),
which is why they live in code and not in Jev. Master-data checks (vendor, PO,
duplicates) need the database and live in jev_invoice.matching.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from jev_invoice.domain.enums import ReasonCode
from jev_invoice.domain.invoice import CheckFinding, ExtractedInvoice
from jev_invoice.domain.vendors import normalize_vendor_name

REQUIRED_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "total")
_FUTURE_GRACE = timedelta(days=1)


@dataclass(frozen=True)
class CheckOutcome:
    """One named check and what it found. No findings means it passed."""

    name: str
    label: str
    findings: tuple[CheckFinding, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.findings


def run_document_checks(
    invoice: ExtractedInvoice,
    *,
    today: date,
    tolerance: Decimal,
    approval_limit: Decimal,
    base_currency: str = "USD",
    stale_after_days: int = 180,
    company_name: str | None = None,
) -> list[CheckOutcome]:
    """Run every document-level check, keeping each result (never short-circuits)."""
    return [
        CheckOutcome(
            "required_fields",
            "All required fields were read",
            tuple(check_required_fields(invoice)),
        ),
        CheckOutcome(
            "arithmetic", "Amounts add up", tuple(check_arithmetic(invoice, tolerance=tolerance))
        ),
        CheckOutcome(
            "dates",
            "Dates make sense",
            tuple(check_dates(invoice, today=today, stale_after_days=stale_after_days)),
        ),
        CheckOutcome(
            "currency",
            f"Invoice is in {base_currency}",
            tuple(check_currency(invoice, base_currency=base_currency)),
        ),
        *(
            [
                CheckOutcome(
                    "bill_to",
                    f"Addressed to {company_name}",
                    tuple(check_bill_to(invoice, company_name=company_name)),
                )
            ]
            if company_name
            else []
        ),
        CheckOutcome(
            "approval_limit",
            f"Within the approval limit ({approval_limit:,.2f})",
            tuple(check_approval_limit(invoice, approval_limit=approval_limit)),
        ),
    ]


def check_extraction(
    invoice: ExtractedInvoice,
    *,
    today: date,
    tolerance: Decimal,
    approval_limit: Decimal,
    base_currency: str = "USD",
    stale_after_days: int = 180,
    company_name: str | None = None,
) -> list[CheckFinding]:
    """Run every document-level check and return all findings (never short-circuits)."""
    outcomes = run_document_checks(
        invoice,
        today=today,
        tolerance=tolerance,
        approval_limit=approval_limit,
        base_currency=base_currency,
        stale_after_days=stale_after_days,
        company_name=company_name,
    )
    return [finding for outcome in outcomes for finding in outcome.findings]


def check_required_fields(invoice: ExtractedInvoice) -> list[CheckFinding]:
    missing = [name for name in REQUIRED_FIELDS if getattr(invoice, name) is None]
    if not missing:
        return []
    return [
        CheckFinding(
            code=ReasonCode.MISSING_REQUIRED_FIELD,
            message=f"Could not read: {', '.join(missing)}",
        )
    ]


def check_arithmetic(invoice: ExtractedInvoice, *, tolerance: Decimal) -> list[CheckFinding]:
    """Lines, subtotal, discount, tax and total must agree within *tolerance*."""
    findings: list[CheckFinding] = []

    for number, line in enumerate(invoice.line_items, start=1):
        quantity, unit_price, amount = line.quantity, line.unit_price, line.amount
        if quantity is None or unit_price is None or amount is None:
            continue
        expected = (quantity * unit_price).quantize(Decimal("0.01"))
        if abs(expected - amount) > tolerance:
            findings.append(
                CheckFinding(
                    code=ReasonCode.TOTALS_MISMATCH,
                    message=(
                        f"Line {number}: {quantity} x {unit_price} = {expected},"
                        f" but the line says {amount}"
                    ),
                )
            )

    line_amounts = [line.amount for line in invoice.line_items if line.amount is not None]
    lines_sum = sum(line_amounts, Decimal(0)) if line_amounts else None

    if (
        invoice.subtotal is not None
        and lines_sum is not None
        and abs(lines_sum - invoice.subtotal) > tolerance
    ):
        findings.append(
            CheckFinding(
                code=ReasonCode.TOTALS_MISMATCH,
                message=f"Lines add up to {lines_sum}, but the subtotal is {invoice.subtotal}",
            )
        )

    base = invoice.subtotal if invoice.subtotal is not None else lines_sum
    if base is not None and invoice.total is not None:
        discount, shipping, tax = invoice.discount or 0, invoice.shipping or 0, invoice.tax or 0
        expected_total = base - discount + shipping + tax
        if abs(expected_total - invoice.total) > tolerance:
            findings.append(
                CheckFinding(
                    code=ReasonCode.TOTALS_MISMATCH,
                    message=(
                        f"{base} - discount {discount} + shipping {shipping} + tax {tax}"
                        f" = {expected_total}, but the total is {invoice.total}"
                    ),
                )
            )

    # A deposit or credit already applied: total - paid must equal the balance due.
    if invoice.total is not None and invoice.amount_due is not None:
        paid = invoice.amount_paid or 0
        expected_due = invoice.total - paid
        if abs(expected_due - invoice.amount_due) > tolerance:
            findings.append(
                CheckFinding(
                    code=ReasonCode.TOTALS_MISMATCH,
                    message=(
                        f"Total {invoice.total} - already paid {paid} = {expected_due},"
                        f" but the balance due is {invoice.amount_due}"
                    ),
                )
            )
    return findings


def check_bill_to(invoice: ExtractedInvoice, *, company_name: str) -> list[CheckFinding]:
    """The invoice must be addressed to us, not to another company.

    Names are compared without legal suffixes ("Northwind Analytics" matches "Northwind
    Analytics Inc"). An invoice with no bill-to printed is not flagged.
    """
    if not invoice.bill_to_name:
        return []
    if normalize_vendor_name(invoice.bill_to_name) == normalize_vendor_name(company_name):
        return []
    return [
        CheckFinding(
            code=ReasonCode.WRONG_BILL_TO,
            message=f"Addressed to '{invoice.bill_to_name}', not {company_name}",
        )
    ]


def check_dates(
    invoice: ExtractedInvoice, *, today: date, stale_after_days: int = 180
) -> list[CheckFinding]:
    findings: list[CheckFinding] = []
    if invoice.invoice_date and invoice.invoice_date < today - timedelta(days=stale_after_days):
        findings.append(
            CheckFinding(
                code=ReasonCode.STALE_INVOICE,
                message=(
                    f"Invoice date {invoice.invoice_date} is more than {stale_after_days} days ago"
                ),
            )
        )
    if invoice.invoice_date and invoice.invoice_date > today + _FUTURE_GRACE:
        findings.append(
            CheckFinding(
                code=ReasonCode.INVALID_DATES,
                message=f"Invoice date {invoice.invoice_date} is in the future",
            )
        )
    if invoice.invoice_date and invoice.due_date and invoice.due_date < invoice.invoice_date:
        findings.append(
            CheckFinding(
                code=ReasonCode.INVALID_DATES,
                message=(
                    f"Due date {invoice.due_date} is before invoice date {invoice.invoice_date}"
                ),
            )
        )
    return findings


def check_currency(invoice: ExtractedInvoice, *, base_currency: str) -> list[CheckFinding]:
    """Only the company's own currency is processed automatically (no FX conversion yet).

    An invoice with no currency printed is assumed to be in the base currency; its amounts
    are still checked against the vendor and the purchase order.
    """
    if invoice.currency and invoice.currency != base_currency:
        return [
            CheckFinding(
                code=ReasonCode.FOREIGN_CURRENCY,
                message=f"The invoice is in {invoice.currency}; only {base_currency} is supported",
            )
        ]
    return []


def check_approval_limit(
    invoice: ExtractedInvoice, *, approval_limit: Decimal
) -> list[CheckFinding]:
    if invoice.total is not None and invoice.total > approval_limit:
        return [
            CheckFinding(
                code=ReasonCode.OVER_APPROVAL_LIMIT,
                message=f"Total {invoice.total} is above the approval limit of {approval_limit}",
            )
        ]
    return []
