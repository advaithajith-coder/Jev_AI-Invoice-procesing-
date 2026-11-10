"""The sample pack: one invoice per outcome, in the formats vendors really send.

Each sample is a text template in `invoices/`. Dates are written relative to the day the
pack is built ({{date-2}} is two days ago), so the invoices are never stale, discount
windows are open, and nothing is dated in the future. The order matters: the resent
Brightline invoice is only a duplicate because the original came first.

One sample passes every check in code and is caught only by Jev: the urgent gift-card
request (Meridian), billed against its PO so nothing else stands out. The instructions
aimed at software (Stratus) are also Jev's catch, but Stratus bills without a PO, which
the code flags too. Stratus may be held for the instructions or blocked, when Jev also
rates the payment risk as high; both are right, since fraud review outranks a hold.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

from jev_invoice.demo.documents import (
    email_with_attachment,
    email_with_body,
    pdf_from_text,
    scan_from_text,
)
from jev_invoice.domain.enums import DecisionAction, ReasonCode

TEMPLATE_DIR = Path(__file__).parent / "invoices"
Form = Literal["pdf", "png", "txt", "email+pdf", "email"]

A = DecisionAction
R = ReasonCode


@dataclass(frozen=True)
class Sample:
    template: str
    form: Form
    shows: str
    expect: tuple[DecisionAction, ...]
    """Any of these is right. Empty: not processed, kept back to upload live."""
    expect_reason: ReasonCode | None = None
    jev_only: bool = False
    """Every check in code passes; only Jev's judgment stops the invoice."""
    email: tuple[str, str] | None = None
    """Sender and subject, for the email forms."""

    @property
    def filename(self) -> str:
        stem = self.template.removesuffix(".txt")
        extension = {"pdf": "pdf", "png": "png", "txt": "txt"}.get(self.form, "eml")
        return f"{stem}.{extension}"

    @property
    def content_type(self) -> str:
        return {
            "pdf": "application/pdf",
            "png": "image/png",
            "txt": "text/plain",
        }.get(self.form, "message/rfc822")


SAMPLES: tuple[Sample, ...] = (
    Sample(
        "brightline-hosting.txt",
        "email+pdf",
        "Clean invoice by email, approved automatically",
        expect=(A.AUTO_APPROVE,),
        email=("Brightline Billing <billing@brightlinecloud.com>", "Invoice BCS-77120"),
    ),
    Sample(
        "acme-office-restock.txt",
        "pdf",
        "Approved automatically, 2% early-payment discount open",
        expect=(A.AUTO_APPROVE,),
    ),
    Sample(
        "harbor-point-rent.txt",
        "png",
        "Scanned rent invoice read on this machine; no PO, so a budget owner signs off",
        expect=(A.ROUTE_APPROVAL, A.HOLD_REVIEW),
        expect_reason=R.NON_PO_APPROVAL_REQUIRED,
    ),
    Sample(
        "halcyon-laptops.txt",
        "pdf",
        "Over the approval limit; laptops also checked as a possible fixed asset",
        expect=(A.ROUTE_APPROVAL, A.HOLD_REVIEW),
        expect_reason=R.OVER_APPROVAL_LIMIT,
    ),
    Sample(
        "clearpath-new-bank.txt",
        "email+pdf",
        "New bank account on the invoice, blocked as possible fraud",
        expect=(A.FRAUD_REVIEW,),
        expect_reason=R.BANK_ACCOUNT_MISMATCH,
        email=("Clearpath Accounts <accounts@clearpathit.com>", "Invoice CP-4410, new bank"),
    ),
    Sample(
        "meridian-urgent-payment.txt",
        "pdf",
        "Pay today in gift cards: only Jev catches it",
        expect=(A.FRAUD_REVIEW,),
        expect_reason=R.UNUSUAL_PAYMENT_METHOD,
        jev_only=True,
    ),
    Sample(
        "stratus-telecom.txt",
        "txt",
        "Hidden instructions aimed at AI: Jev flags them",
        expect=(A.HOLD_REVIEW, A.FRAUD_REVIEW),
        expect_reason=R.PROMPT_INJECTION,
    ),
    Sample(
        "brightline-hosting-resent.txt",
        "pdf",
        "The Brightline invoice sent again, rejected as a duplicate",
        expect=(A.REJECT_DOCUMENT,),
        expect_reason=R.DUPLICATE_INVOICE,
    ),
    Sample(
        "acme-lookalike-domain.txt",
        "email",
        "Emailed from acme-supplies.co, a lookalike domain",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.SENDER_DOMAIN_MISMATCH,
        email=("Acme Supplies Billing <billing@acme-supplies.co>", "Invoice AS-31055"),
    ),
    Sample(
        "acme-credit-note.txt",
        "pdf",
        "Credit note for returned toner, to apply to AS-31010",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.CREDIT_NOTE,
    ),
    # Error invoices: each is written to trip one check. PO-1003 is billed by several of
    # them, so the order matters; the lookalike AS-31O10 comes after the rest and also
    # pushes PO-1003 past what was delivered, which is what a double bill would do.
    Sample(
        "pixelforge-wrong-po.txt",
        "pdf",
        "Pixelforge quotes Acme's PO-1003",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.PO_VENDOR_MISMATCH,
    ),
    Sample(
        "acme-price-increase.txt",
        "pdf",
        "Toner billed at 94.00, the PO says 89.50",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.PRICE_VARIANCE,
    ),
    Sample(
        "acme-extra-items.txt",
        "pdf",
        "Staplers billed that were never on the PO",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.LINE_NOT_ON_PO,
    ),
    Sample(
        "acme-wrong-tax-rate.txt",
        "pdf",
        "Goods taxed at 7% instead of Massachusetts' 6.25%",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.TAX_REVIEW_REQUIRED,
    ),
    Sample(
        "acme-number-typo.txt",
        "pdf",
        "AS-31O10 (letter O): the same bill as AS-31010 again",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.POSSIBLE_DUPLICATE,
    ),
    Sample(
        "acme-cancelled-po.txt",
        "pdf",
        "Billed against PO-1009, which was cancelled",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.PO_CLOSED,
    ),
    Sample(
        "halcyon-servers-not-received.txt",
        "pdf",
        "Two servers billed, only one delivered",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.QUANTITY_VARIANCE,
    ),
    Sample(
        "pixelforge-over-po.txt",
        "pdf",
        "Extra media spend takes the bill past PO-1005",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.PO_AMOUNT_EXCEEDED,
    ),
    Sample(
        "pixelforge-no-po.txt",
        "pdf",
        "No PO from a vendor that needs one, sent for approval",
        expect=(A.ROUTE_APPROVAL, A.HOLD_REVIEW),
        expect_reason=R.NO_PURCHASE_ORDER,
    ),
    Sample(
        "northline-unknown-po.txt",
        "pdf",
        "Quotes PO-1099, which does not exist",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.PO_NOT_FOUND,
    ),
    Sample(
        "meridian-math-error.txt",
        "pdf",
        "Lines add up to 2,200.00 but the total says 2,250.00",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.TOTALS_MISMATCH,
    ),
    Sample(
        "stratus-wrong-bill-to.txt",
        "pdf",
        "Addressed to Northwind Logistics Ltd, not to us",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.WRONG_BILL_TO,
    ),
    Sample(
        "harbor-point-tax-id.txt",
        "pdf",
        "Tax ID 26-5550124; the one on file is 26-5550142",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.TAX_ID_MISMATCH,
    ),
    Sample(
        "oldmill-on-hold.txt",
        "pdf",
        "Old Mill Stationers is on hold",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.VENDOR_ON_HOLD,
    ),
    Sample(
        "vortex-blocked.txt",
        "pdf",
        "Vortex Trading is blocked",
        expect=(A.FRAUD_REVIEW,),
        expect_reason=R.VENDOR_BLOCKED,
    ),
    Sample(
        "summit-unknown-vendor.txt",
        "pdf",
        "A vendor that was never set up",
        expect=(A.HOLD_REVIEW,),
        expect_reason=R.UNKNOWN_VENDOR,
    ),
    Sample(
        "northline-courier.txt",
        "pdf",
        "Kept back: upload it live during the demo",
        expect=(),
    ),
)

_PLACEHOLDER = re.compile(r"\{\{(date|month|ym)([+-]\d+)?\}\}")


def render_text(template: str, today: date) -> str:
    """Fill in {{date-N}} / {{date+N}} (MM/DD/YYYY), {{month}} and {{ym}}."""

    def fill(match: re.Match[str]) -> str:
        kind, offset = match.group(1), int(match.group(2) or 0)
        if kind == "date":
            return (today + timedelta(days=offset)).strftime("%m/%d/%Y")
        if kind == "month":
            return today.strftime("%B %Y")
        return today.strftime("%Y-%m")

    return _PLACEHOLDER.sub(fill, template)


def render(sample: Sample, today: date) -> bytes:
    text = render_text((TEMPLATE_DIR / sample.template).read_text(encoding="utf-8"), today)
    sender, subject = sample.email or ("", "")
    match sample.form:
        case "pdf":
            return pdf_from_text(text)
        case "png":
            return scan_from_text(text)
        case "txt":
            return text.encode()
        case "email":
            return email_with_body(sender=sender, subject=subject, body=text)
        case "email+pdf":
            body = "Hello,\n\nPlease find our invoice attached.\n\nKind regards,\nAccounts"
            name = sample.template.removesuffix(".txt") + ".pdf"
            return email_with_attachment(
                sender=sender,
                subject=subject,
                body=body,
                attachment=pdf_from_text(text),
                filename=name,
            )


def write_pack(directory: Path, today: date | None = None) -> list[tuple[Sample, Path]]:
    """Render every sample into `directory` (emptied first) and return the files in order."""
    today = today or datetime.now(UTC).date()
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.iterdir():
        if old.is_file():
            old.unlink()
    written = []
    for sample in SAMPLES:
        path = directory / sample.filename
        path.write_bytes(render(sample, today))
        written.append((sample, path))
    return written
