"""Approval guard for documents whose reading did not finish."""

from jev_invoice.core.errors import IncompleteDocumentError
from jev_invoice.db.models import Invoice


def require_complete_document(invoice: Invoice) -> None:
    coverage = (invoice.additional_info or {}).get("document_coverage", {})
    if coverage.get("status") == "incomplete":
        raise IncompleteDocumentError(
            coverage.get("reason", "Document reading is incomplete.")
            + " Reject this incomplete copy and upload a complete readable version."
        )
