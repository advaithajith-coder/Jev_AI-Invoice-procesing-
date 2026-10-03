"""Master-data checks against the seeded database (see db/seed_data.py for scenarios)."""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.config import Settings
from jev_invoice.db.models import Invoice
from jev_invoice.domain.enums import InvoiceSource, InvoiceStatus, ReasonCode
from jev_invoice.domain.invoice import ExtractedInvoice
from jev_invoice.matching.service import MatchingService, MatchResult, normalize_reference
from tests.fakes import ACME_BANK, acme_invoice_fields

R = ReasonCode


async def _match(session: AsyncSession, settings: Settings, **overrides: Any) -> MatchResult:
    invoice = ExtractedInvoice.model_validate(acme_invoice_fields(**overrides))
    service = MatchingService(
        session,
        fingerprint_key=settings.bank_fingerprint_key.get_secret_value().encode(),
        price_tolerance_pct=Decimal("2.0"),
    )
    return await service.match(invoice, invoice_id=uuid.uuid4())


def _codes(result: MatchResult) -> list[ReasonCode]:
    return [finding.code for finding in result.findings]


async def test_clean_invoice_matches_vendor_po_and_bank(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(session, settings)

    assert _codes(result) == []
    assert result.vendor is not None and result.vendor.name == "Acme Supplies Inc"
    assert result.purchase_order is not None and result.purchase_order.po_number == "PO-1003"
    assert result.bank_account_last4 == "6819"
    assert result.bank_account_fingerprint == result.vendor.bank_account_fingerprint


async def test_vendor_name_variants_match(session: AsyncSession, settings: Settings) -> None:
    result = await _match(session, settings, vendor_name="ACME SUPPLIES LIMITED.")
    assert result.vendor is not None


async def test_lookalike_vendor_is_unknown(session: AsyncSession, settings: Settings) -> None:
    result = await _match(session, settings, vendor_name="Acme Suppl1es Ltd")

    assert R.UNKNOWN_VENDOR in _codes(result)
    assert result.vendor is None


async def test_changed_bank_account_is_flagged(session: AsyncSession, settings: Settings) -> None:
    result = await _match(session, settings, bank_account="GB11 FAKE 0400 7519 2837 46")

    assert _codes(result) == [R.BANK_ACCOUNT_MISMATCH]
    assert "ends 2746" not in result.findings[0].message
    assert "ending 3746" in result.findings[0].message and "6819" in result.findings[0].message


async def test_same_account_formatted_differently_matches(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(session, settings, bank_account=ACME_BANK.replace(" ", "-").lower())
    assert _codes(result) == []


@pytest.mark.parametrize(
    ("vendor_name", "code"),
    [("Old Mill Stationers", R.VENDOR_ON_HOLD), ("Vortex Trading LLC", R.VENDOR_BLOCKED)],
)
async def test_vendor_status(
    session: AsyncSession, settings: Settings, vendor_name: str, code: ReasonCode
) -> None:
    result = await _match(
        session, settings, vendor_name=vendor_name, po_reference=None, bank_account=None
    )
    assert code in _codes(result)


async def test_currency_differs_from_vendor_and_po(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(session, settings, currency="EUR")
    assert _codes(result).count(R.CURRENCY_MISMATCH) == 2


async def test_no_po_reference(session: AsyncSession, settings: Settings) -> None:
    assert _codes(await _match(session, settings, po_reference=None)) == [R.NO_PURCHASE_ORDER]


async def test_po_reference_formats_normalised(session: AsyncSession, settings: Settings) -> None:
    result = await _match(session, settings, po_reference="po 1003")
    assert result.purchase_order is not None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"po_reference": "PO-9999"}, [R.PO_NOT_FOUND]),
        (
            {"po_reference": "PO-1004"},
            [R.PO_VENDOR_MISMATCH, R.LINE_NOT_ON_PO],
        ),  # Brightline's PO: toner is not on it
        (
            {"po_reference": "PO-1009"},
            [R.PO_CLOSED, R.LINE_NOT_ON_PO, R.GOODS_NOT_RECEIVED],
        ),  # cancelled chair order, none received
    ],
)
async def test_purchase_order_problems(
    session: AsyncSession, settings: Settings, overrides: dict[str, Any], expected: list[ReasonCode]
) -> None:
    assert _codes(await _match(session, settings, **overrides)) == expected


async def test_amount_above_po_total(session: AsyncSession, settings: Settings) -> None:
    # PO-1003 is for 2,323.50 (all received). 2,400.00 is over the 2% tolerance.
    result = await _match(session, settings, subtotal="2400.00", total="2400.00", line_items=[])
    assert _codes(result) == [R.PO_AMOUNT_EXCEEDED, R.GOODS_NOT_RECEIVED]


async def test_amount_within_tolerance(session: AsyncSession, settings: Settings) -> None:
    result = await _match(session, settings, subtotal="2369.00", total="2369.00", line_items=[])
    assert _codes(result) == []  # 2,323.50 x 1.02 = 2,369.97


async def test_goods_not_received(session: AsyncSession, settings: Settings) -> None:
    # PO-1002: two servers ordered, one received (4,800.00). Invoice is for both.
    result = await _match(
        session,
        settings,
        vendor_name="Halcyon Computing LLC",
        po_reference="PO-1002",
        bank_account=None,
        subtotal="9600.00",
        total="9600.00",
        line_items=[],
    )
    assert _codes(result) == [R.GOODS_NOT_RECEIVED]


async def test_discount_reduces_amount_checked_against_po(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(
        session, settings, subtotal="2400.00", discount="100.00", total="2300.00", line_items=[]
    )
    assert _codes(result) == []


async def _store_invoice(
    session: AsyncSession,
    *,
    number: str,
    status: InvoiceStatus,
    net: str,
    vendor_id: uuid.UUID,
    po_id: uuid.UUID,
) -> None:
    session.add(
        Invoice(
            status=status,
            source=InvoiceSource.UPLOAD,
            original_filename="earlier.pdf",
            content_type="application/pdf",
            file_size_bytes=10,
            file_sha256=uuid.uuid4().hex * 2,
            storage_key="x",
            vendor_id=vendor_id,
            purchase_order_id=po_id,
            invoice_number=number,
            invoice_number_normalized=normalize_reference(number),
            subtotal=Decimal(net),
            total=Decimal(net),
        )
    )
    await session.flush()


async def test_duplicate_invoice_number_from_same_vendor(
    session: AsyncSession, settings: Settings
) -> None:
    first = await _match(session, settings)
    assert first.vendor is not None and first.purchase_order is not None
    await _store_invoice(
        session,
        number="as 20431",
        status=InvoiceStatus.APPROVED,
        net="10.00",
        vendor_id=first.vendor.id,
        po_id=first.purchase_order.id,
    )

    assert R.DUPLICATE_INVOICE in _codes(await _match(session, settings))


async def test_rejected_invoices_are_not_duplicates(
    session: AsyncSession, settings: Settings
) -> None:
    first = await _match(session, settings)
    assert first.vendor is not None and first.purchase_order is not None
    await _store_invoice(
        session,
        number="AS-20431",
        status=InvoiceStatus.REJECTED,
        net="2000.00",
        vendor_id=first.vendor.id,
        po_id=first.purchase_order.id,
    )

    assert _codes(await _match(session, settings)) == []


async def test_earlier_invoices_count_against_the_po(
    session: AsyncSession, settings: Settings
) -> None:
    first = await _match(session, settings)
    assert first.vendor is not None and first.purchase_order is not None
    await _store_invoice(
        session,
        number="AS-20400",
        status=InvoiceStatus.APPROVED,
        net="1249.50",
        vendor_id=first.vendor.id,
        po_id=first.purchase_order.id,
    )

    # 1,249.50 already billed + 1,074.00 now = 2,323.50: exactly the PO.
    assert _codes(await _match(session, settings)) == []
    over = await _match(session, settings, subtotal="1200.00", total="1200.00", line_items=[])
    assert R.PO_AMOUNT_EXCEEDED in _codes(over)
