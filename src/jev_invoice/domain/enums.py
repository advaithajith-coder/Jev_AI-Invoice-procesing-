"""Closed sets of values used across the database, API, and decision policy."""

from enum import StrEnum


class VendorStatus(StrEnum):
    ACTIVE = "active"
    ON_HOLD = "on_hold"
    BLOCKED = "blocked"


class GLAccountType(StrEnum):
    EXPENSE = "expense"
    ASSET = "asset"
    COST_OF_SALES = "cost_of_sales"


class PurchaseOrderStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class InvoiceSource(StrEnum):
    UPLOAD = "upload"
    EMAIL = "email"


class InvoiceStatus(StrEnum):
    """Lifecycle of an invoice. Each transition is recorded as an audit event."""

    RECEIVED = "received"
    EXTRACTED = "extracted"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPORTED = "exported"
    FAILED = "failed"


class DocumentType(StrEnum):
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    STATEMENT = "statement"
    QUOTE = "quote"
    RECEIPT = "receipt"
    OTHER = "other"


class DecisionAction(StrEnum):
    """What should happen to the invoice next."""

    AUTO_APPROVE = "auto_approve"  # approve without a person
    ROUTE_APPROVAL = "route_approval"  # a budget owner approves
    HOLD_REVIEW = "hold_review"  # a reviewer checks the document or the data
    REJECT_DOCUMENT = "reject_document"  # not payable (quote, statement, duplicate, ...)
    FRAUD_REVIEW = "fraud_review"  # do not pay; escalate


class ActionBand(StrEnum):
    """How much human involvement a decision needs before its action runs."""

    AUTO = "auto"  # runs without a person
    CONFIRM = "confirm"  # a person confirms the proposed action
    HUMAN = "human"  # a person decides
    BLOCK = "block"  # do not act; escalate


class ReasonCode(StrEnum):
    """Why an invoice cannot simply be approved. Code checks and Jev both emit these."""

    # Extraction and arithmetic (code)
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INCOMPLETE_DOCUMENT = "INCOMPLETE_DOCUMENT"
    TOTALS_MISMATCH = "TOTALS_MISMATCH"
    INVALID_DATES = "INVALID_DATES"
    WRONG_BILL_TO = "WRONG_BILL_TO"
    STALE_INVOICE = "STALE_INVOICE"
    FOREIGN_CURRENCY = "FOREIGN_CURRENCY"
    # Master data (code)
    UNKNOWN_VENDOR = "UNKNOWN_VENDOR"
    VENDOR_ON_HOLD = "VENDOR_ON_HOLD"
    VENDOR_BLOCKED = "VENDOR_BLOCKED"
    BANK_ACCOUNT_MISMATCH = "BANK_ACCOUNT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    SENDER_DOMAIN_MISMATCH = "SENDER_DOMAIN_MISMATCH"
    TAX_ID_MISMATCH = "TAX_ID_MISMATCH"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    # Purchase order matching (code)
    NO_PURCHASE_ORDER = "NO_PURCHASE_ORDER"
    PO_NOT_FOUND = "PO_NOT_FOUND"
    PO_VENDOR_MISMATCH = "PO_VENDOR_MISMATCH"
    PO_CLOSED = "PO_CLOSED"
    PO_AMOUNT_EXCEEDED = "PO_AMOUNT_EXCEEDED"
    GOODS_NOT_RECEIVED = "GOODS_NOT_RECEIVED"
    INVOICE_BEFORE_PO = "INVOICE_BEFORE_PO"
    LINE_NOT_ON_PO = "LINE_NOT_ON_PO"
    PRICE_VARIANCE = "PRICE_VARIANCE"
    QUANTITY_VARIANCE = "QUANTITY_VARIANCE"
    # Approval rules (code)
    OVER_APPROVAL_LIMIT = "OVER_APPROVAL_LIMIT"
    NON_PO_APPROVAL_REQUIRED = "NON_PO_APPROVAL_REQUIRED"
    GL_CODING_REVIEW = "GL_CODING_REVIEW"
    # Decision model availability
    JEV_NOT_CONFIGURED = "JEV_NOT_CONFIGURED"
    JEV_UNAVAILABLE = "JEV_UNAVAILABLE"
    # Judgments (Jev)
    NOT_AN_INVOICE = "NOT_AN_INVOICE"
    CREDIT_NOTE = "CREDIT_NOTE"
    NOT_PAYABLE = "NOT_PAYABLE"
    PROMPT_INJECTION = "PROMPT_INJECTION"
    BANK_DETAILS_CHANGED = "BANK_DETAILS_CHANGED"
    URGENT_PAYMENT_PRESSURE = "URGENT_PAYMENT_PRESSURE"
    UNUSUAL_PAYMENT_METHOD = "UNUSUAL_PAYMENT_METHOD"
    HIGH_PAYMENT_RISK = "HIGH_PAYMENT_RISK"
    MODERATE_PAYMENT_RISK = "MODERATE_PAYMENT_RISK"
    JEV_UNCERTAIN = "JEV_UNCERTAIN"
    FINANCE_CLASSIFICATION_REVIEW = "FINANCE_CLASSIFICATION_REVIEW"
    TAX_REVIEW_REQUIRED = "TAX_REVIEW_REQUIRED"
    ACCOUNTING_PERIOD_REVIEW = "ACCOUNTING_PERIOD_REVIEW"
    ACCOUNTING_TREATMENT_REVIEW = "ACCOUNTING_TREATMENT_REVIEW"


class ReviewStatus(StrEnum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
