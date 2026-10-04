"""The payables page: approved invoices with what is still owed, in tabs. Old addresses
redirect here.

Dates are relative to today. Seed data: Acme is on 2/10 net 30 against PO-1003;
Meridian Legal is net 14 and bills without a PO.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from tests.fakes import acme_invoice_fields, install_fakes, wire_answers

PERSON = {"person": "Maya"}


def _days_ago(days: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


def _approve(app: FastAPI, client: TestClient, settings: Settings, **overrides: Any) -> str:
    fields = acme_invoice_fields(due_date=None, **overrides)
    install_fakes(app, settings, fields, jev=[wire_answers()])
    number = overrides["invoice_number"]
    body = client.post(
        "/api/v1/invoices", files={"file": (f"{number}.txt", number.encode(), "text/plain")}
    ).json()
    assert body["status"] == "approved", body["decision"]["reason_codes"]
    return str(body["id"])


def _meridian(app: FastAPI, client: TestClient, settings: Settings, **overrides: Any) -> str:
    return _approve(
        app,
        client,
        settings,
        vendor_name="Meridian Legal LLP",
        po_reference=None,
        bank_account=None,
        line_items=[
            {
                "description": "Contract review",
                "quantity": 1,
                "unit_price": 1074.00,
                "amount": 1074.00,
            }
        ],
        **overrides,
    )


def test_empty_page(client: TestClient) -> None:
    page = client.get("/payables").text

    assert "Nothing owed" in page
    assert 'aria-current="page">Payables</a>' in page
    for removed in ("Payment due schedule", "Received, not yet billed", "Discounts to keep"):
        assert removed not in page


def _count(page: str, tab: str) -> str:
    return page.split(f'>{tab} <span class="count">', 1)[1].split("<", 1)[0]


def test_open_discount_is_planned_for_its_last_day(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    # Dated 2 days ago: the 10-day discount is open for 8 more days.
    _approve(app, client, seeded_settings, invoice_number="AS-1", invoice_date=_days_ago(2))

    page = client.get("/payables").text
    discount_tab = client.get("/payables?tab=discount").text

    deadline = datetime.now(UTC).date() + timedelta(days=8)
    assert "USD 1,074.00" in page  # owed
    assert f"{deadline.day} {deadline:%b %Y}" in page
    assert "keep 2%" in page and "without the discount" in page
    assert "AS-1" in discount_tab and _count(page, "Discount open") == "1"


def test_late_invoice_is_due_now(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    # Net 14: dated 50 days ago, it is 36 days late.
    _meridian(app, client, seeded_settings, invoice_number="ML-1", invoice_date=_days_ago(50))

    late = client.get("/payables?tab=late").text

    assert "36 days late" in late and "<b>Now</b>" in late
    assert _count(late, "Late") == "1"


def test_due_this_week_and_missed_discount(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    # Meridian dated 10 days ago is due in 4 days. Acme dated 12 days ago missed its
    # discount (it ended 2 days ago) and is due in 18 days.
    _meridian(app, client, seeded_settings, invoice_number="ML-2", invoice_date=_days_ago(10))
    _approve(app, client, seeded_settings, invoice_number="AS-2", invoice_date=_days_ago(12))

    page = client.get("/payables").text
    week = client.get("/payables?tab=week").text

    assert "in 4 days" in page and "in 18 days" in page
    assert "ML-2" in week and "AS-2" not in week
    assert _count(page, "Discount open") == "0"


def test_held_invoices_have_their_own_tab(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    held = _meridian(app, client, seeded_settings, invoice_number="ML-3", invoice_date=_days_ago(1))
    _approve(app, client, seeded_settings, invoice_number="AS-3", invoice_date=_days_ago(1))
    client.post(f"/invoices/{held}/hold", data={**PERSON, "kind": "hold", "reason": "Budget"})

    page = client.get("/payables").text
    held_tab = client.get("/payables?tab=held").text
    discount_tab = client.get("/payables?tab=discount").text

    assert "when released" in page
    assert "ML-3" in held_tab and "AS-3" not in held_tab
    assert "AS-3" in discount_tab and "ML-3" not in discount_tab


def test_csv_of_the_current_tab(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    _meridian(app, client, seeded_settings, invoice_number="ML-4", invoice_date=_days_ago(50))

    page = client.get("/payables?tab=late").text
    csv = client.get("/payables/invoices.csv?tab=late").text

    assert 'href="/payables/invoices.csv?tab=late"' in page
    assert csv.splitlines()[0].startswith("Vendor,Invoice,Invoice date,Pay by,Days late")
    assert "ML-4" in csv and ",36," in csv


def test_payments_leave_the_open_list_for_the_paid_tab(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    invoice_id = _meridian(
        app, client, seeded_settings, invoice_number="ML-5", invoice_date=_days_ago(3)
    )
    client.post(
        f"/invoices/{invoice_id}/payment",
        data={**PERSON, "amount": "1074.00", "method": "ach"},
    )

    assert "Nothing owed" in client.get("/payables").text
    assert "ML-5" in client.get("/payables?tab=paid").text


def test_old_addresses_redirect(client: TestClient) -> None:
    for old in ("/approved", "/reports/aging", "/reports/cash-forecast", "/reports/accruals"):
        response = client.get(old, follow_redirects=False)
        assert response.status_code == 308 and response.headers["location"] == "/payables"
    assert client.get("/payables/accruals.csv").status_code == 404


def test_each_tab_has_a_totals_row(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    # Acme with its 2% discount open, and a Meridian bill that gets paid.
    _approve(app, client, seeded_settings, invoice_number="AS-6", invoice_date=_days_ago(2))
    paid = _meridian(app, client, seeded_settings, invoice_number="ML-6", invoice_date=_days_ago(3))
    _meridian(app, client, seeded_settings, invoice_number="ML-7", invoice_date=_days_ago(12))
    client.post(f"/invoices/{paid}/payment", data={**PERSON, "amount": "1074.00", "method": "ach"})

    open_totals = client.get("/payables").text.split('<tfoot class="totals">')[1]
    paid_totals = client.get("/payables?tab=paid").text.split('<tfoot class="totals">')[1]
    held = client.get("/payables?tab=held").text

    assert "2 invoices" in open_totals and "USD 2,148.00" in open_totals
    assert "2,126.52 if paid on these dates, keeping 21.48 in discounts" in open_totals
    assert "1 invoice<" in paid_totals and "USD 1,074.00" in paid_totals
    assert 'class="totals"' not in held  # an empty tab has no totals row
