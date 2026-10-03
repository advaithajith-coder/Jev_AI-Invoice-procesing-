"""Sender domain and vendor tax ID: flagged only when known and wrong, never when missing.

Acme Supplies Inc is on file with email domain acmesupplies.com and EIN 84-1234567.
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.config import Settings
from jev_invoice.domain.enums import ReasonCode
from jev_invoice.domain.invoice import ExtractedInvoice
from jev_invoice.matching.service import MatchingService, MatchResult
from tests.fakes import acme_invoice_fields

R = ReasonCode


async def _match(
    session: AsyncSession, settings: Settings, sender: str | None = None, **overrides: Any
) -> MatchResult:
    invoice = ExtractedInvoice.model_validate(acme_invoice_fields(**overrides))
    return await MatchingService(
        session,
        fingerprint_key=settings.bank_fingerprint_key.get_secret_value().encode(),
        price_tolerance_pct=Decimal("2.0"),
    ).match(invoice, invoice_id=uuid.uuid4(), sender_email=sender)


def _codes(result: MatchResult) -> list[ReasonCode]:
    return [finding.code for finding in result.findings]


def _check(result: MatchResult, name: str) -> bool | str | None:
    """The check's result, or "absent" when it was not shown at all."""
    return next((check.passed for check in result.checks if check.name == name), "absent")


@pytest.mark.parametrize("sender", ["billing@acmesupplies.com", "AR@Invoices.AcmeSupplies.com"])
async def test_vendor_domain_and_its_subdomains_pass(
    session: AsyncSession, settings: Settings, sender: str
) -> None:
    result = await _match(session, settings, sender=sender)

    assert R.SENDER_DOMAIN_MISMATCH not in _codes(result)
    assert _check(result, "sender") is True


async def test_lookalike_domain_is_flagged(session: AsyncSession, settings: Settings) -> None:
    result = await _match(session, settings, sender="billing@acme-supplies.co")

    assert R.SENDER_DOMAIN_MISMATCH in _codes(result)
    message = next(f.message for f in result.findings if f.code == R.SENDER_DOMAIN_MISMATCH)
    assert "looks like acmesupplies.com" in message


async def test_unrelated_domain_is_flagged(session: AsyncSession, settings: Settings) -> None:
    result = await _match(session, settings, sender="pay@gmail.com")
    assert R.SENDER_DOMAIN_MISMATCH in _codes(result)


async def test_no_sender_is_not_flagged(session: AsyncSession, settings: Settings) -> None:
    result = await _match(session, settings, sender=None)  # uploaded, not emailed

    assert R.SENDER_DOMAIN_MISMATCH not in _codes(result)
    assert _check(result, "sender") == "absent"


@pytest.mark.parametrize("printed", ["84-1234567", "841234567", "EIN 84 1234567"])
async def test_matching_tax_id_passes_whatever_the_format(
    session: AsyncSession, settings: Settings, printed: str
) -> None:
    result = await _match(session, settings, vendor_tax_id=printed.replace("EIN ", ""))

    assert R.TAX_ID_MISMATCH not in _codes(result)
    assert _check(result, "tax_id") is True


async def test_different_tax_id_is_flagged(session: AsyncSession, settings: Settings) -> None:
    result = await _match(session, settings, vendor_tax_id="91-7654321")

    assert R.TAX_ID_MISMATCH in _codes(result)
    assert "84-1234567" in next(f.message for f in result.findings if f.code == R.TAX_ID_MISMATCH)


async def test_no_tax_id_on_the_invoice_is_not_flagged(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(session, settings, vendor_tax_id=None)

    assert R.TAX_ID_MISMATCH not in _codes(result)
    assert _check(result, "tax_id") == "absent"


async def test_vendor_without_records_is_not_flagged(
    session: AsyncSession, settings: Settings
) -> None:
    # Vortex Trading has no email domain and no tax ID on file (and is blocked anyway).
    result = await _match(
        session,
        settings,
        sender="ap@somewhere-else.com",
        vendor_name="Vortex Trading LLC",
        vendor_tax_id="11-1111111",
    )

    assert R.SENDER_DOMAIN_MISMATCH not in _codes(result)
    assert R.TAX_ID_MISMATCH not in _codes(result)
