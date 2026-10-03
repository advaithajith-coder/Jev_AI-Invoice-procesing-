"""Plain-language text for codes the pipeline produces.

Reviewers see these, not the raw codes. Each reason says what was found and what
to do about it. `tone` drives colour: block (red), human (amber), confirm (blue).
"""

from dataclasses import dataclass
from typing import Literal

from jev_invoice.domain.enums import ActionBand, DecisionAction, InvoiceStatus, ReasonCode

Tone = Literal["block", "human", "confirm", "ok", "muted"]


@dataclass(frozen=True)
class Reason:
    title: str
    guidance: str
    tone: Tone


R = ReasonCode

REASONS: dict[ReasonCode, Reason] = {
    R.BANK_ACCOUNT_MISMATCH: Reason(
        "Bank account differs from our records",
        "The invoice asks for payment to an account that is not the one on file. Call the "
        "vendor on a number you already have, never one printed on this invoice.",
        "block",
    ),
    R.VENDOR_BLOCKED: Reason(
        "Vendor is blocked", "Payments to this vendor are stopped. Do not pay.", "block"
    ),
    R.BANK_DETAILS_CHANGED: Reason(
        "Announces new bank details",
        "A common payment-fraud pattern. Confirm any change with the vendor on a known number.",
        "block",
    ),
    R.UNUSUAL_PAYMENT_METHOD: Reason(
        "Unusual payment method",
        "Gift cards, cryptocurrency or a personal account are not normal for a business invoice.",
        "block",
    ),
    R.HIGH_PAYMENT_RISK: Reason(
        "High payment risk", "The document reads like one that should not be paid.", "block"
    ),
    R.PROMPT_INJECTION: Reason(
        "Contains instructions aimed at software",
        "The text tries to steer automated processing. Check every field against the document.",
        "human",
    ),
    R.JEV_NOT_CONFIGURED: Reason(
        "Decision model not connected",
        "Jev is not configured, so every invoice is decided by a person.",
        "human",
    ),
    R.JEV_UNAVAILABLE: Reason(
        "Decision model did not answer",
        "Jev timed out, hit its rate limit, or returned an error, so a person decides.",
        "human",
    ),
    R.JEV_UNCERTAIN: Reason(
        "Decision model unsure",
        "At least one of Jev's answers was in the grey zone. See its judgment below.",
        "human",
    ),
    R.NOT_AN_INVOICE: Reason(
        "Not an invoice",
        "Looks like a quote, statement or receipt. Confirm, then reject it.",
        "confirm",
    ),
    R.NOT_PAYABLE: Reason(
        "Does not ask for payment",
        "Nothing here requests payment. Confirm, then reject.",
        "confirm",
    ),
    R.DUPLICATE_INVOICE: Reason(
        "Duplicate invoice",
        "This vendor's invoice number was received before. Paying it again would pay twice.",
        "confirm",
    ),
    R.CREDIT_NOTE: Reason(
        "Credit note",
        "Reduces an earlier invoice. Apply it to the vendor's balance instead of paying it.",
        "human",
    ),
    R.MISSING_REQUIRED_FIELD: Reason(
        "Fields could not be read",
        "Fill in or check the missing values from the document.",
        "human",
    ),
    R.TOTALS_MISMATCH: Reason(
        "Amounts do not add up",
        "Lines, discount, tax and total disagree. Check the document for errors or misreads.",
        "human",
    ),
    R.INVALID_DATES: Reason(
        "Dates look wrong",
        "The invoice is dated in the future or due before it was issued.",
        "human",
    ),
    R.UNKNOWN_VENDOR: Reason(
        "Vendor not in the vendor list",
        "Could be a new supplier, or a lookalike name. Check before adding them.",
        "human",
    ),
    R.VENDOR_ON_HOLD: Reason(
        "Vendor is on hold", "Payments to this vendor need a manager's say-so.", "human"
    ),
    R.SENDER_DOMAIN_MISMATCH: Reason(
        "Emailed from a different domain",
        "The invoice did not come from the vendor's email domain. A lookalike domain is a"
        " classic fraud sign. Some vendors send through a billing service: confirm with the"
        " vendor on a number you already have before paying.",
        "human",
    ),
    R.TAX_ID_MISMATCH: Reason(
        "Tax ID differs from our records",
        "The tax ID (EIN) on the invoice is not the one on file for this vendor. It may be a"
        " fake invoice, or a misread number: compare with the vendor's W-9.",
        "human",
    ),
    R.WRONG_BILL_TO: Reason(
        "Addressed to another company",
        "The invoice is billed to a different company. It may belong to a sister company, or"
        " be a fake built from someone else's invoice. Do not pay it as ours.",
        "human",
    ),
    R.STALE_INVOICE: Reason(
        "Very old invoice",
        "Dated long ago: it may be late, already paid, or re-sent. Check the vendor's statement.",
        "human",
    ),
    R.POSSIBLE_DUPLICATE: Reason(
        "Possible duplicate",
        "Same vendor and amount as another recent invoice, or an almost identical number."
        " Compare the two before paying; re-sent invoices often get a new number.",
        "human",
    ),
    R.INVOICE_BEFORE_PO: Reason(
        "Invoice dated before its purchase order",
        "The PO may have been raised after the purchase to get it paid. Ask the buyer why.",
        "human",
    ),
    R.LINE_NOT_ON_PO: Reason(
        "Line not on the purchase order",
        "A charge on the invoice matches nothing that was ordered. Ask the buyer to approve it"
        " or the vendor to remove it.",
        "human",
    ),
    R.PRICE_VARIANCE: Reason(
        "Price above the purchase order",
        "A line is billed above the agreed PO price by more than the tolerance.",
        "human",
    ),
    R.QUANTITY_VARIANCE: Reason(
        "More billed than received",
        "A line bills more units than were delivered (or ordered). Pay only for what arrived.",
        "human",
    ),
    R.FOREIGN_CURRENCY: Reason(
        "Not in US dollars",
        "Only USD invoices are processed automatically. Check the amount and how it will be"
        " paid before approving.",
        "human",
    ),
    R.CURRENCY_MISMATCH: Reason(
        "Unexpected currency",
        "The currency differs from the vendor's usual one or the purchase order's.",
        "human",
    ),
    R.PO_NOT_FOUND: Reason(
        "Purchase order not found", "The PO number quoted does not exist in our records.", "human"
    ),
    R.PO_VENDOR_MISMATCH: Reason(
        "Purchase order belongs to another vendor",
        "The quoted PO was issued to a different supplier.",
        "human",
    ),
    R.PO_CLOSED: Reason(
        "Purchase order closed or cancelled", "Nothing more should be billed against it.", "human"
    ),
    R.PO_AMOUNT_EXCEEDED: Reason(
        "More than the purchase order allows",
        "Together with earlier invoices, this bills more than was ordered.",
        "human",
    ),
    R.GOODS_NOT_RECEIVED: Reason(
        "Not yet delivered",
        "We are billed for more than has been received. Check with whoever ordered it.",
        "human",
    ),
    R.URGENT_PAYMENT_PRESSURE: Reason(
        "Pressure to pay urgently",
        "Unusual urgency is a fraud signal. Do not let it shortcut checks.",
        "human",
    ),
    R.MODERATE_PAYMENT_RISK: Reason(
        "Some payment risk", "Jev rated the risk as moderate. Read the document closely.", "human"
    ),
    R.OVER_APPROVAL_LIMIT: Reason(
        "Above the approval limit", "A budget owner must approve amounts this large.", "confirm"
    ),
    R.NO_PURCHASE_ORDER: Reason(
        "No purchase order",
        "Spend without a PO needs a budget owner's approval.",
        "confirm",
    ),
}

ACTIONS: dict[DecisionAction, tuple[str, Tone]] = {
    DecisionAction.AUTO_APPROVE: ("Approved automatically", "ok"),
    DecisionAction.ROUTE_APPROVAL: ("Needs budget approval", "confirm"),
    DecisionAction.HOLD_REVIEW: ("Needs a reviewer", "human"),
    DecisionAction.REJECT_DOCUMENT: ("Proposed: reject", "confirm"),
    DecisionAction.FRAUD_REVIEW: ("Blocked: possible fraud", "block"),
}

BANDS: dict[ActionBand, str] = {
    ActionBand.AUTO: "No person needed: every check passed and Jev saw no risk.",
    ActionBand.CONFIRM: "The system proposes an action. A person confirms it or overrules it.",
    ActionBand.HUMAN: "A person decides. The reasons below say what to check.",
    ActionBand.BLOCK: (
        "Payment is blocked. Approving needs a written note saying how it was verified."
    ),
}

STATUSES: dict[InvoiceStatus, tuple[str, Tone]] = {
    InvoiceStatus.RECEIVED: ("Received", "muted"),
    InvoiceStatus.EXTRACTED: ("Read", "muted"),
    InvoiceStatus.PENDING_REVIEW: ("Waiting for review", "human"),
    InvoiceStatus.APPROVED: ("Approved", "ok"),
    InvoiceStatus.REJECTED: ("Rejected", "muted"),
    InvoiceStatus.EXPORTED: ("Exported", "ok"),
    InvoiceStatus.FAILED: ("Could not read", "block"),
}

AUDIT_ACTIONS: dict[str, str] = {
    "settings.approval_limit_changed": "Approval limit changed",
    "invoice.received": "Received",
    "invoice.transcribed": "Image transcribed",
    "invoice.extracted": "Fields read",
    "invoice.extraction_failed": "Could not be read",
    "decision.made": "Decision made",
    "invoice.auto_approved": "Approved automatically",
    "review.opened": "Sent for review",
    "review.approved": "Approved by reviewer",
    "review.rejected": "Rejected by reviewer",
    "invoice.exported": "Exported to accounting",
    "vendor.created": "Vendor added",
    "vendor.updated": "Vendor details changed",
    "vendor.bank_account_changed": "Bank account changed",
    "po.created": "Purchase order created",
    "po.delivery_recorded": "Delivery recorded",
    "po.closed": "Purchase order closed",
    "po.cancelled": "Purchase order cancelled",
}

VENDOR_STATUSES: dict[str, tuple[str, Tone]] = {
    "active": ("Active", "ok"),
    "on_hold": ("On hold", "human"),
    "blocked": ("Blocked", "block"),
}

PO_STATUSES: dict[str, tuple[str, Tone]] = {
    "open": ("Open, nothing received", "confirm"),
    "partially_received": ("Partly received", "human"),
    "received": ("Fully received", "ok"),
    "closed": ("Closed", "muted"),
    "cancelled": ("Cancelled", "muted"),
}

JEV_SIGNALS: tuple[tuple[str, str], ...] = (
    ("prompt_injection", "Instructions aimed at software"),
    ("bank_details_change", "Announces new bank details"),
    ("urgent_pressure", "Pressure to pay urgently"),
    ("unusual_payment_method", "Unusual payment method"),
)

DOCUMENT_TYPES: dict[str, str] = {
    "invoice": "Invoice",
    "credit_note": "Credit note",
    "statement": "Statement",
    "quote": "Quote",
    "receipt": "Receipt",
    "other": "Other",
}


def reason(code: str) -> Reason:
    try:
        return REASONS[ReasonCode(code)]
    except (ValueError, KeyError):
        return Reason(code.replace("_", " ").capitalize(), "", "human")
