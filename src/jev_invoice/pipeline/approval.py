"""Evidence required for automatic approval, on processing and record rechecks."""

from jev_invoice.db.models import Invoice
from jev_invoice.domain.enums import ReasonCode
from jev_invoice.domain.invoice import CheckFinding


def approval_findings(invoice: Invoice, *, suspense_account: str) -> list[CheckFinding]:
    findings: list[CheckFinding] = []
    if invoice.purchase_order_id is None:
        findings.append(
            CheckFinding(
                code=ReasonCode.NON_PO_APPROVAL_REQUIRED,
                message="No matched purchase order: a Budget owner must approve the business "
                "purpose, even when the vendor does not require a PO.",
            )
        )
    if not invoice.lines:
        findings.append(
            CheckFinding(
                code=ReasonCode.GL_CODING_REVIEW,
                message="No invoice lines establish trusted GL coding; Finance must review.",
            )
        )
    for line in invoice.lines:
        if not line.gl_account_code or line.gl_account_code == suspense_account:
            reason = "Missing or suspense GL account"
        elif (
            line.gl_source == "po" and line.purchase_order_line_id is not None
        ) or line.gl_source == "validated_correction":
            continue
        else:
            reason = f"GL coding from {line.gl_source or 'an unknown source'}"
        findings.append(
            CheckFinding(
                code=ReasonCode.GL_CODING_REVIEW,
                message=f"Line {line.line_no}: {reason}. Automatic approval requires a matched "
                "PO line account or validated correction; Finance must confirm this coding.",
            )
        )
    return findings
