"""Exceptions and payments: hold, dispute, comments, reassign, short-pay, credit notes,
payments with early-payment discounts, and coding lines left uncoded.

Seed data: Acme Supplies Inc is on 2/10 net 30 and PO-1003 is fully received; Meridian
Legal LLP bills without a PO on net 14.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from tests.fakes import acme_invoice_fields, install_fakes, wire_answers

PERSON = {"person": "Maya"}


def _days_ago(days: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


def _upload(
    app: FastAPI, client: TestClient, settings: Settings, answers: Any = None, **fields: Any
) -> dict[str, Any]:
    data = acme_invoice_fields(due_date=None, invoice_date=_days_ago(2), **fields)
    install_fakes(app, settings, data, jev=[answers or wire_answers()])
    number = data["invoice_number"]
    body: dict[str, Any] = client.post(
        "/api/v1/invoices", files={"file": (f"{number}.txt", number.encode(), "text/plain")}
    ).json()
    return body


def _approved(app: FastAPI, client: TestClient, settings: Settings, **fields: Any) -> str:
    body = _upload(app, client, settings, **fields)
    assert body["status"] == "approved", body["decision"]["reason_codes"]
    return str(body["id"])


def _post(client: TestClient, invoice_id: str, action: str, **data: Any) -> Any:
    return client.post(
        f"/invoices/{invoice_id}/{action}", data={**PERSON, **data}, follow_redirects=False
    )


def test_hold_blocks_payment_and_export_until_released(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    invoice_id = _approved(app, client, seeded_settings, invoice_number="AS-90001")

    held = _post(client, invoice_id, "hold", kind="hold", reason="Waiting for the delivery note")
    page = client.get(held.headers["location"]).text
    pay = _post(client, invoice_id, "payment", amount="1074.00", method="ach")
    exports = client.get("/exports").text

    assert held.status_code == 303
    assert "On hold" in page and "Waiting for the delivery note" in page
    assert "Payment is blocked while the invoice is on hold" in page
    assert pay.status_code == 422 and "on hold" in pay.text
    assert "AS-90001" in exports and "on hold" in exports  # listed as not ready

    released = _post(client, invoice_id, "release", outcome="other", note="Note arrived")
    page = client.get(released.headers["location"]).text
    assert "Hold released" in page and "Record a payment" in page


def test_dispute_drafts_a_vendor_message_and_needs_an_outcome(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    invoice_id = _approved(app, client, seeded_settings, invoice_number="AS-90002")

    no_category = _post(client, invoice_id, "hold", kind="dispute", reason="Too much")
    opened = _post(
        client,
        invoice_id,
        "hold",
        kind="dispute",
        category="price",
        reason="Toner billed at $89.50; we agreed $85.00.",
    )
    page = client.get(opened.headers["location"]).text

    assert no_category.status_code == 422 and "what the dispute is about" in no_category.text
    assert "Disputed with the vendor: price is higher than agreed" in page
    assert "Hello Acme Supplies Inc," in page  # the drafted message
    assert "Accounts Payable, Northwind Analytics Inc" in page

    silent = _post(client, invoice_id, "release", outcome="credit_coming", note="")
    resolved = _post(client, invoice_id, "release", outcome="credit_coming", note="CN promised")
    page = client.get(resolved.headers["location"]).text
    assert silent.status_code == 422 and "what the vendor agreed" in silent.text
    assert "Dispute resolved" in page and "vendor is sending a credit note" in page


def test_comment_needs_a_name_and_shows_in_history(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    invoice_id = _approved(app, client, seeded_settings, invoice_number="AS-90003")

    nameless = client.post(f"/invoices/{invoice_id}/comment", data={"text": "Hi"})
    added = _post(client, invoice_id, "comment", text="Checked with the warehouse: all 12 arrived.")
    page = client.get(added.headers["location"]).text

    assert nameless.status_code == 400 and "Enter your name" in nameless.text
    assert "Comment added" in page and "all 12 arrived" in page


def test_reassign_shows_in_the_queue_and_held_filter(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    # No PO for a vendor that needs one: goes to review.
    body = _upload(app, client, seeded_settings, invoice_number="AS-90004", po_reference=None)
    assert body["status"] == "pending_review"

    assigned = _post(client, body["id"], "assign", assignee="Jordan", note="Your budget")
    queue = client.get("/review").text
    held_before = client.get("/review?show=held").text
    _post(client, body["id"], "hold", kind="hold", reason="Waiting for the PO")
    held_after = client.get("/review?show=held").text

    assert assigned.status_code == 303
    assert "Jordan" in queue and "AS-90004" in queue
    assert "Assigned to me" not in queue and "Unassigned" not in queue
    assert "AS-90004" not in held_before
    assert "AS-90004" in held_after


def test_short_pay_then_a_credit_note_settles_the_withheld_part(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    invoice_id = _approved(app, client, seeded_settings, invoice_number="AS-90005")

    no_reason = _post(client, invoice_id, "short-pay", amount="89.50", reason="")
    too_much = _post(client, invoice_id, "short-pay", amount="2000", reason="x")
    short = _post(client, invoice_id, "short-pay", amount="89.50", reason="One cartridge damaged")
    page = client.get(short.headers["location"]).text

    assert no_reason.status_code == 422 and too_much.status_code == 422
    assert "One cartridge damaged" in page and "USD 984.50" in page  # balance to pay

    credit = _post(client, invoice_id, "credit", number="CN-555", amount="89.50", reason="Damaged")
    page = client.get(credit.headers["location"]).text
    assert "Credit CN-555" in page
    assert "Short-pay</dt>" not in page  # the credit settled the withheld part
    assert "USD 984.50" in page  # still owed: not reduced twice

    over = _post(client, invoice_id, "credit", number="CN-556", amount="5000")
    assert over.status_code == 422 and "more than is still owed" in over.text


def test_uploaded_credit_note_is_applied_from_its_page(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    invoice_id = _approved(app, client, seeded_settings, invoice_number="AS-90006")
    credit = _upload(
        app,
        client,
        seeded_settings,
        answers=wire_answers(document_type="credit_note"),
        invoice_number="CN-7001",
        po_reference=None,
        subtotal=179.00,
        total=179.00,
        line_items=[
            {
                "description": "Credit: 2 toner cartridges returned",
                "quantity": 2,
                "unit_price": 89.50,
                "amount": 179.00,
            }
        ],
    )
    assert credit["status"] == "pending_review"

    page = client.get(f"/invoices/{credit['id']}").text
    assert "Apply USD 179.00 credit" in page and "AS-90006" in page
    assert 'value="approve"' not in page  # a credit note is applied, not approved

    # The original invoice offers the waiting credit note too.
    assert "1 from this vendor waiting" in client.get(f"/invoices/{invoice_id}").text

    applied = _post(client, credit["id"], "apply-to", target_id=invoice_id)
    page = client.get(applied.headers["location"]).text
    assert applied.headers["location"].startswith(f"/invoices/{invoice_id}")
    assert "Credit CN-7001" in page and "USD 895.00" in page

    note_page = client.get(f"/invoices/{credit['id']}").text
    assert "Applied to" in note_page and "AS-90006" in note_page
    assert "CN-7001" not in client.get("/review").text  # its review closed
    assert "CN-7001" not in client.get("/payables").text  # never shown as payable
    again = _post(client, invoice_id, "credit", credit_note_id=credit["id"])
    assert again.status_code == 422 and "already been applied" in again.text


def test_payment_with_early_discount_marks_the_invoice_paid(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    invoice_id = _approved(app, client, seeded_settings, invoice_number="AS-90007")
    page = client.get(f"/invoices/{invoice_id}").text
    assert 'value="1052.52"' in page  # 1,074.00 less the 2% discount (21.48)

    future = _post(
        client,
        invoice_id,
        "payment",
        paid_on=(datetime.now(UTC).date() + timedelta(days=3)).isoformat(),
        amount="1052.52",
        method="ach",
        take_discount="1",
    )
    over = _post(client, invoice_id, "payment", amount="1074.00", method="ach", take_discount="1")
    paid = _post(
        client,
        invoice_id,
        "payment",
        amount="1052.52",
        method="ach",
        reference="ACH-4411",
        take_discount="1",
    )
    page = client.get(paid.headers["location"]).text

    assert future.status_code == 422 and "once they are made" in future.text
    assert over.status_code == 422 and "more than is still owed" in over.text
    assert "Payment recorded" in page and "paid in full" in page
    assert "Discount kept" in page and "ACH-4411" in page
    assert "AS-90007" not in client.get("/payables").text
    assert "AS-90007" in client.get("/payables?tab=paid").text


def test_part_payment_leaves_a_balance(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    invoice_id = _approved(app, client, seeded_settings, invoice_number="AS-90008")

    _post(client, invoice_id, "payment", amount="500.00", method="check", reference="1042")
    page = client.get(f"/invoices/{invoice_id}").text
    dashboard = client.get("/payables").text

    assert "Part paid" in page and "USD 574.00" in page
    assert "574.00" in dashboard and "Part paid" in dashboard


def test_code_with_jev_codes_lines_without_an_account(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body = _upload(
        app,
        client,
        seeded_settings,
        vendor_name="Meridian Legal LLP",
        invoice_number="ML-900",
        po_reference=None,
        bank_account=None,
        line_items=[
            {
                "description": "Contract review",
                "quantity": 4,
                "unit_price": 268.50,
                "amount": 1074.00,
            }
        ],
    )
    database = seeded_settings.database_url.split("///", 1)[1]
    with sqlite3.connect(database) as db:  # as if read before coding existed
        db.execute("UPDATE invoice_lines SET gl_account_code = NULL, gl_source = NULL")
    assert "Code with Jev" in client.get(f"/invoices/{body['id']}").text

    install_fakes(app, seeded_settings, coding={"line_1": ("6100", 0.9)})
    coded = _post(client, body["id"], "code")
    page = client.get(coded.headers["location"]).text

    assert "Lines coded" in page and 'value="6100" selected' in page
    assert "Code with Jev" not in page
    assert "Lines coded by Jev" in page
