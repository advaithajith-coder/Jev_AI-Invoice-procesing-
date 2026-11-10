"""Accounts-payable rules as companies run them (see db/seed_data.py for the scenarios):
PO-free vendors, line-by-line 3-way matching with "2% or $5" tolerances, near-duplicates,
date red flags, agreed payment terms, and the approval warnings this demo shows."""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.config import Settings
from jev_invoice.domain.enums import ReasonCode
from jev_invoice.domain.invoice import ExtractedInvoice
from jev_invoice.matching.service import MatchingService, MatchResult
from tests.fakes import acme_invoice_fields, install_fakes, wire_answers

R = ReasonCode
TONER = "Toner cartridge, black, high yield"  # PO-1003 line 2: 12 ordered and received at 89.50


def _service(session: AsyncSession, settings: Settings) -> MatchingService:
    return MatchingService(
        session,
        fingerprint_key=settings.bank_fingerprint_key.get_secret_value().encode(),
        price_tolerance_pct=settings.po_price_tolerance_pct,
        tolerance_max_amount=settings.po_tolerance_max_amount,
    )


async def _match(session: AsyncSession, settings: Settings, **overrides: Any) -> MatchResult:
    invoice = ExtractedInvoice.model_validate(acme_invoice_fields(**overrides))
    return await _service(session, settings).match(invoice, invoice_id=uuid.uuid4())


def _codes(result: MatchResult) -> list[ReasonCode]:
    return [finding.code for finding in result.findings]


def _toner(quantity: int, price: str) -> dict[str, Any]:
    amount = str((Decimal(quantity) * Decimal(price)).quantize(Decimal("0.01")))
    return {
        "subtotal": amount,
        "total": amount,
        "line_items": [
            {"description": TONER, "quantity": quantity, "unit_price": price, "amount": amount}
        ],
    }


# ── Tolerance: a percentage or a fixed cap, whichever is smaller ──────────────


def test_tolerance_is_the_smaller_of_percent_and_cap(settings: Settings) -> None:
    service = _service(None, settings)  # type: ignore[arg-type]

    assert service.allowed(Decimal("100.00")) == Decimal("2.00")  # 2% is smaller
    assert service.allowed(Decimal("1000000.00")) == Decimal("5.00")  # not $20,000


# ── PO required, or not ───────────────────────────────────────────────────────


async def test_vendor_billed_without_po_needs_none(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(
        session, settings, vendor_name="Meridian Legal LLP", po_reference=None, bank_account=None
    )

    assert R.NO_PURCHASE_ORDER not in _codes(result)
    po_check = next(check for check in result.checks if check.name == "po")
    assert po_check.passed is None and "bills without a PO" in po_check.detail


async def test_vendor_that_uses_pos_still_needs_one(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(session, settings, po_reference=None)

    assert _codes(result) == [R.NO_PURCHASE_ORDER]


# ── Line-by-line 3-way match ──────────────────────────────────────────────────


async def test_matching_lines_pass_and_are_paired(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(session, settings)

    assert _codes(result) == []
    assert list(result.line_matches) == [0]
    checks = {check.name: check.passed for check in result.checks}
    assert checks["lines_on_po"] and checks["line_prices"] and checks["line_received"]
    assert "po_amount" not in checks  # the line match replaces the totals comparison


async def test_small_price_rise_within_tolerance_passes(
    session: AsyncSession, settings: Settings
) -> None:
    # 12 x 0.40 = 4.80 over; allowed is the $5 cap (2% of 1,074.00 would be 21.48)
    result = await _match(session, settings, **_toner(12, "89.90"))
    assert R.PRICE_VARIANCE not in _codes(result)


async def test_price_above_po_is_flagged(session: AsyncSession, settings: Settings) -> None:
    # 12 x 5.50 = 66.00 over: above both 2% (21.48) and the $5 cap
    result = await _match(session, settings, **_toner(12, "95.00"))

    assert R.PRICE_VARIANCE in _codes(result)
    message = next(f.message for f in result.findings if f.code == R.PRICE_VARIANCE)
    assert "95.00" in message and "89.50" in message


async def test_more_billed_than_received_is_flagged(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(session, settings, **_toner(15, "89.50"))

    assert R.QUANTITY_VARIANCE in _codes(result)
    assert "only 12 received" in next(
        f.message for f in result.findings if f.code == R.QUANTITY_VARIANCE
    )


async def test_line_not_on_the_po_is_flagged(session: AsyncSession, settings: Settings) -> None:
    fields = _toner(12, "89.50")
    fields["line_items"].append(
        {
            "description": "Expedited delivery fee",
            "quantity": 1,
            "unit_price": "45.00",
            "amount": "45.00",
        }
    )
    fields["subtotal"] = fields["total"] = "1119.00"

    result = await _match(session, settings, **fields)

    assert R.LINE_NOT_ON_PO in _codes(result)
    assert list(result.line_matches) == [0]  # the toner line still pairs


# ── Dates ─────────────────────────────────────────────────────────────────────


async def test_invoice_dated_before_its_po_is_flagged(
    session: AsyncSession, settings: Settings
) -> None:
    result = await _match(session, settings, invoice_date="2026-08-20")  # PO-1003 issued 9/1
    assert R.INVOICE_BEFORE_PO in _codes(result)


# ── Through the pipeline: quantities across invoices, near-duplicates ─────────


def _upload(
    app: FastAPI, client: TestClient, settings: Settings, **overrides: Any
) -> dict[str, Any]:
    install_fakes(app, settings, acme_invoice_fields(**overrides), jev=[wire_answers()])
    number = overrides.get("invoice_number", "AS-1")
    body: dict[str, Any] = client.post(
        "/api/v1/invoices", files={"file": (f"{number}.txt", number.encode(), "text/plain")}
    ).json()
    return body


def test_quantities_add_up_across_invoices(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    first = _upload(app, client, seeded_settings, invoice_number="AS-31", **_toner(8, "89.50"))
    second = _upload(app, client, seeded_settings, invoice_number="AS-32", **_toner(8, "89.50"))

    assert "QUANTITY_VARIANCE" not in first["decision"]["reason_codes"]
    assert "QUANTITY_VARIANCE" in second["decision"]["reason_codes"]  # 16 billed, 12 received


@pytest.mark.parametrize(
    ("second", "why"),
    [
        ({"invoice_number": "AS-77002", "invoice_date": "2026-10-03"}, "a date close to it"),
        (
            {"invoice_number": "AS-7700l", "invoice_date": "2026-12-15"},
            "an almost identical number",
        ),
    ],
)
def test_possible_duplicates_are_flagged(
    app: FastAPI, client: TestClient, seeded_settings: Settings, second: dict[str, Any], why: str
) -> None:
    _upload(app, client, seeded_settings, invoice_number="AS-77001", invoice_date="2026-09-30")
    body = _upload(app, client, seeded_settings, **second)

    assert "POSSIBLE_DUPLICATE" in body["decision"]["reason_codes"]
    assert "DUPLICATE_INVOICE" not in body["decision"]["reason_codes"]


def test_same_amount_a_month_later_is_not_a_duplicate(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    _upload(app, client, seeded_settings, invoice_number="AS-88001", invoice_date="2026-08-30")
    body = _upload(
        app, client, seeded_settings, invoice_number="AS-88102", invoice_date="2026-09-30"
    )

    assert "POSSIBLE_DUPLICATE" not in body["decision"]["reason_codes"]  # e.g. a monthly bill


# ── Payment terms and approval warnings on the invoice page ──────────────────


def test_invoice_page_shows_agreed_terms_and_discount(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    # Acme is on 2/10 net 30; this invoice asks for payment in 7 days.
    body = _upload(
        app, client, seeded_settings, invoice_number="AS-51", invoice_date="2026-09-30",
        due_date="2026-10-07",
    )  # fmt: skip

    page = client.get(f"/invoices/{body['id']}").text

    assert "agreed terms: 2/10 net 30" in page
    assert "30 Oct 2026" in page  # pay by: invoice date + 30, not the 7 days asked for
    assert "earlier than the agreed terms" in page
    assert "2% early-payment discount" in page and "USD 21.48" in page


def test_review_warns_about_controls_a_company_would_enforce(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    vendors = client.get("/api/v1/vendors").json()
    acme = next(v for v in vendors if v["name"] == "Acme Supplies Inc")
    client.post(
        f"/vendors/{acme['id']}/bank-account",
        data={
            "bank_account": "Routing 021000021 Account 4417926819",
            "note": "Called Acme on the number we already had; confirmed.",
            "person": "Sam",
        },
    )  # sets the reviewer cookie to Sam
    body = _upload(app, client, seeded_settings, invoice_number="AS-61", po_reference=None)
    assert body["status"] == "pending_review"

    page = client.get(f"/invoices/{body['id']}").text
    approved = client.post(
        f"/invoices/{body['id']}/review",
        data={"task_id": _open_task(client, body["id"]), "verdict": "approve", "reviewer": "Sam"},
        follow_redirects=False,
    )
    history = client.get(f"/invoices/{body['id']}").text

    assert "Demo: anyone can approve" in page
    assert "You changed this vendor" in page and "separation of duties" in page
    assert approved.status_code == 303  # a warning, not a block
    assert "Approved by reviewer" in history


def _open_task(client: TestClient, invoice_id: str) -> str:
    tasks = client.get("/api/v1/review").json()
    task_id: str = next(task["id"] for task in tasks if task["invoice"]["id"] == invoice_id)
    return task_id
