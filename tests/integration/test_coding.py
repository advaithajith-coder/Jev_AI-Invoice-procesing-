"""Account coding: PO accounts first, then Jev's confident choice, then the vendor's
default, then suspense; people can recode; the chart of accounts drives Jev's options.

Seed data: PO-1003's lines are coded 6000 (Office Supplies). Meridian Legal bills
without a PO and defaults to 6100 (Legal Fees).
"""

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from tests.fakes import FakeJevServer, acme_invoice_fields, install_fakes, wire_answers

LEGAL_LINE = {
    "description": "Commercial contract review, September",
    "quantity": 4,
    "unit_price": "275.00",
    "amount": "1100.00",
}


def _meridian(**overrides: Any) -> dict[str, Any]:
    fields = acme_invoice_fields(
        vendor_name="Meridian Legal LLP",
        invoice_number="ML-301",
        po_reference=None,
        bank_account=None,
        subtotal="1100.00",
        total="1100.00",
        line_items=[LEGAL_LINE],
    )
    fields.update(overrides)
    return fields


def _process(
    app: FastAPI,
    client: TestClient,
    settings: Settings,
    fields: dict[str, Any],
    coding: dict[str, tuple[str, float]] | None = None,
    jev_configured: bool = True,
) -> tuple[Any, FakeJevServer]:
    _, server = install_fakes(
        app, settings, fields, jev=[wire_answers()], coding=coding, jev_configured=jev_configured
    )
    number = fields["invoice_number"]
    body = client.post(
        "/api/v1/invoices", files={"file": (f"{number}.txt", number.encode(), "text/plain")}
    ).json()
    return body, server


def _coding_step(client: TestClient, invoice_id: str) -> str:
    page: str = client.get(f"/invoices/{invoice_id}").text
    return page


def test_po_lines_keep_the_po_account_without_asking_jev(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, server = _process(app, client, seeded_settings, acme_invoice_fields())

    page = client.get(f"/invoices/{body['id']}").text
    assert server.coding_requests == []  # nothing left for Jev to code
    assert "from the PO" in page
    assert 'value="6000" selected' in page


def test_confident_jev_choice_is_used(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, server = _process(
        app, client, seeded_settings, _meridian(), coding={"line_1": ("6100", 0.93)}
    )

    page = client.get(f"/invoices/{body['id']}").text
    assert len(server.coding_requests) == 1
    question = server.coding_requests[0]["questions"]["line_1"]
    assert "6100" in question["criteria"] and "9999" not in question["criteria"]
    assert "chosen by Jev" in page and "Jev 93% sure" in page
    assert 'value="6100" selected' in page
    assert "6100 Legal Fees (chosen by Jev)" in _coding_step(client, body["id"])


def test_unsure_jev_falls_back_to_the_vendor_default(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(app, client, seeded_settings, _meridian(), coding={"line_1": ("6000", 0.41)})

    page = client.get(f"/invoices/{body['id']}").text
    journey = _coding_step(client, body["id"])
    assert "vendor default" in page and "Jev 41% sure" in page
    assert 'value="6100" selected' in page
    assert "Jev suggested 6000 Office Supplies" in journey


def test_unknown_vendor_and_unsure_jev_need_coding(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        _meridian(vendor_name="Brand New Consulting LLC", invoice_number="BN-1"),
        coding={"line_1": ("6100", 0.2)},
    )

    page = client.get(f"/invoices/{body['id']}").text
    assert "needs coding" in page
    assert 'value="9999" selected' in page


def test_without_jev_the_vendor_default_is_used(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(app, client, seeded_settings, _meridian(), jev_configured=False)

    page = client.get(f"/invoices/{body['id']}").text
    assert "vendor default" in page and 'value="6100" selected' in page


def test_a_person_can_recode_a_line(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(app, client, seeded_settings, _meridian())
    page = client.get(f"/invoices/{body['id']}").text
    line_id = page.split('name="account_', 1)[1].split('"', 1)[0]

    refused = client.post(f"/invoices/{body['id']}/accounts", data={f"account_{line_id}": "6110"})
    saved = client.post(
        f"/invoices/{body['id']}/accounts",
        data={f"account_{line_id}": "6110", "person": "Maya"},
        follow_redirects=False,
    )
    after = client.get(saved.headers["location"]).text

    assert refused.status_code == 400 and "Enter your name" in refused.text
    assert saved.status_code == 303
    assert 'value="6110" selected' in after and "set by a person" in after
    assert "Accounts changed" in after  # history


def test_chart_of_accounts_add_edit_and_deactivate(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    added = client.post(
        "/accounts",
        data={
            "code": "6250",
            "name": "Cleaning and janitorial",
            "description": "Office cleaning contracts, janitorial supplies and waste collection.",
            "account_type": "expense",
            "person": "Maya",
        },
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert "Cleaning and janitorial" in client.get("/accounts").text

    _, server = _process(app, client, seeded_settings, _meridian(invoice_number="ML-302"))
    assert "6250" in server.coding_requests[0]["questions"]["line_1"]["criteria"]

    client.post(
        "/accounts/6250",
        data={
            "name": "Cleaning and janitorial",
            "description": "Office cleaning contracts, janitorial supplies and waste collection.",
            "account_type": "expense",
            "person": "Maya",
        },  # is_active unticked
    )
    _, server = _process(app, client, seeded_settings, _meridian(invoice_number="ML-303"))
    assert "6250" not in server.coding_requests[0]["questions"]["line_1"]["criteria"]
    assert "inactive" in client.get("/accounts/6250").text


def test_short_description_and_duplicate_code_are_refused(client: TestClient) -> None:
    short = client.post(
        "/accounts",
        data={"code": "6251", "name": "X", "description": "too short", "person": "Maya"},
    )
    duplicate = client.post(
        "/accounts",
        data={
            "code": "6000",
            "name": "Again",
            "description": "A duplicate of the office supplies account.",
            "person": "Maya",
        },
    )

    assert short.status_code == 400
    assert duplicate.status_code == 400 and "already exists" in duplicate.text


def test_chart_of_accounts_shows_approved_spend(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    _process(app, client, seeded_settings, acme_invoice_fields(invoice_number="AS-80001"))

    page = client.get("/accounts").text
    row = page.split('href="/accounts/6000"', 1)[1].split("</tr>", 1)[0]

    assert "Office Supplies" in row and "1,074.00" in row
