"""Seed data must be internally consistent, or later matching tests would test bad fixtures."""

from collections import Counter
from decimal import Decimal

from jev_invoice.db.seed_data import GL_ACCOUNTS, PURCHASE_ORDERS, VENDORS
from jev_invoice.domain.enums import PurchaseOrderStatus
from jev_invoice.domain.vendors import normalize_vendor_name

GL_CODES = {a.code for a in GL_ACCOUNTS}
VENDOR_KEYS = {v.key for v in VENDORS}


def test_natural_keys_are_unique() -> None:
    def dupes(values: list[str]) -> list[str]:
        return [v for v, n in Counter(values).items() if n > 1]

    assert not dupes([a.code for a in GL_ACCOUNTS])
    assert not dupes([v.key for v in VENDORS])
    assert not dupes([normalize_vendor_name(v.name) for v in VENDORS])
    assert not dupes([po.po_number for po in PURCHASE_ORDERS])
    assert not dupes([r.receipt_number for po in PURCHASE_ORDERS for r in po.receipts])


def test_references_resolve() -> None:
    assert {v.default_gl for v in VENDORS} <= GL_CODES
    for po in PURCHASE_ORDERS:
        assert po.vendor_key in VENDOR_KEYS
        assert {line.gl for line in po.lines} <= GL_CODES
        for receipt in po.receipts:
            assert set(receipt.quantities) <= set(range(1, len(po.lines) + 1))
            assert receipt.received_date >= po.issued_date


def test_receipts_never_exceed_ordered_quantity() -> None:
    for po in PURCHASE_ORDERS:
        for line_no, line in enumerate(po.lines, start=1):
            received = sum((r.quantities.get(line_no, Decimal(0)) for r in po.receipts), Decimal(0))
            assert received <= line.quantity, f"{po.po_number} line {line_no}"


def test_po_status_matches_receipts() -> None:
    for po in PURCHASE_ORDERS:
        ordered = sum((line.quantity for line in po.lines), Decimal(0))
        received = sum((q for r in po.receipts for q in r.quantities.values()), Decimal(0))
        if po.status is PurchaseOrderStatus.RECEIVED:
            assert received == ordered, po.po_number
        elif po.status is PurchaseOrderStatus.PARTIALLY_RECEIVED:
            assert Decimal(0) < received < ordered, po.po_number
        elif po.status in (PurchaseOrderStatus.OPEN, PurchaseOrderStatus.CANCELLED):
            assert received == 0, po.po_number


def test_po_total_is_sum_of_lines() -> None:
    po = next(p for p in PURCHASE_ORDERS if p.po_number == "PO-1003")
    assert po.total == Decimal("50") * Decimal("24.99") + Decimal("12") * Decimal("89.50")


def test_currencies_are_iso_codes() -> None:
    assert all(len(v.currency) == 3 and v.currency.isupper() for v in VENDORS)
