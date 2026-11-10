from jev_invoice.extraction.service import supported_po_reference


def test_po_reference_requires_a_printed_label_and_quotation() -> None:
    assert supported_po_reference(
        "Purchase order: ORDER-412", "ORDER-412", "Purchase order: ORDER-412"
    )
    assert not supported_po_reference(
        "Customer reference: ACCOUNT-412", "ACCOUNT-412", "Customer reference: ACCOUNT-412"
    )
    assert not supported_po_reference(
        "Customer reference: ACCOUNT-412", "ACCOUNT-412", "PO: ACCOUNT-412"
    )
    assert not supported_po_reference("PO: ORDER-412", "ORDER-412", None)
