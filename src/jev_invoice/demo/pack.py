"""The sample pack: one invoice per outcome, in the formats vendors really send.

Each sample is a text template in `invoices/`. Dates are written relative to the day the
pack is built ({{date-2}} is two days ago), so the invoices are never stale, discount
windows are open, and nothing is dated in the future. The order matters: the resent
Brightline invoice is only a duplicate because the original came first.

Two samples pass every check in code and are caught only by Jev: the urgent gift-card
request (Meridian) and the instructions aimed at software (Stratus). Stratus may be held
for the instructions or blocked, when Jev also rates the payment risk as high; both are
right, since fraud review outranks a hold.
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
        "Scanned rent invoice read by the vision model, no PO needed",
        expect=(A.AUTO_APPROVE,),
    ),
    Sample(
        "halcyon-laptops.txt",
        "pdf",
        "Over the approval limit, sent to a budget owner",
        expect=(A.ROUTE_APPROVAL,),
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
        "Hidden instructions aimed at AI: only Jev catches them",
        expect=(A.HOLD_REVIEW, A.FRAUD_REVIEW),
        expect_reason=R.PROMPT_INJECTION,
        jev_only=True,
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
