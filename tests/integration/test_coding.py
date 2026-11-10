"""Account coding: PO accounts first, then Jev's confident choice, then the vendor's
default, then suspense; people can recode; the chart of accounts drives Jev's options.

Seed data: PO-1003's lines are coded 6000 (Office Supplies). Meridian Legal bills
without a PO and defaults to 6100 (Legal Fees).
"""

from typing import Any

import pytest
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
    assert body["status"] == "approved"
    assert "from the PO" in page
    assert 'value="6000" selected' in page


@pytest.mark.parametrize("confidence", [0.41, 0.60, 0.93])
def test_non_po_spend_and_suggested_coding_require_human_review(
    app: FastAPI, client: TestClient, seeded_settings: Settings, confidence: float
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        _meridian(),
        coding={
            "line_1": ("6100", confidence),
            "route_0": ("delivery", 0.99),
            "route_1": ("purchasing", 0.99),
        },
    )
    assert body["status"] == "pending_review"
    assert {"NON_PO_APPROVAL_REQUIRED", "GL_CODING_REVIEW"} <= set(body["decision"]["reason_codes"])
    routes = {r["code"]: r for r in body["decision"]["exception_routing"]}
    assert routes["NON_PO_APPROVAL_REQUIRED"]["owner"] == "Budget owner"
    assert routes["GL_CODING_REVIEW"]["owner"] == "Finance"


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


def test_finance_classification_requires_evidence_and_records_requirements(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        _meridian(),
        coding={
            "finance_1_purchase_category": ("professional_service", 0.95),
            "finance_1_service_dates_required": ("yes", 0.95),
            "finance_1_asset_details_required": ("no", 0.95),
            "finance_1_purchase_category_evidence": ("line", 0.95),
            "finance_1_purchase_category_support": ("supported", 0.95),
            "finance_1_service_dates_required_evidence": ("line", 0.95),
            "finance_1_service_dates_required_support": ("supported", 0.95),
            "finance_1_asset_details_required_evidence": ("line", 0.95),
            "finance_1_asset_details_required_support": ("supported", 0.95),
        },
    )
    page = client.get(f"/invoices/{body['id']}").text
    assert "FINANCE_CLASSIFICATION_REVIEW" in body["decision"]["reason_codes"]
    assert (
        body["lines"][0]["finance_classification"]["fields"]["purchase_category"]["value"]
        == "professional_service"
    )
    assert "professional_service" in page
    assert "Missing service start" in page and "Missing service end" in page
    assert "Jev suggestion" in page and "Evidence: Commercial contract review" in page
    assert "Insufficient evidence" in page


def test_finance_does_not_accept_confident_choice_without_evidence(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        _meridian(),
        coding={
            "finance_1_purchase_category": ("goods", 0.99),
            "finance_1_purchase_category_evidence": ("unknown", 0.99),
        },
    )
    page = client.get(f"/invoices/{body['id']}").text
    assert "Insufficient evidence for purchase category" in page
    category = body["lines"][0]["finance_classification"]["fields"]["purchase_category"]
    assert category["suggested_value"] == "goods"
    assert category["evidence_choice"] == "unknown"
    assert category["evidence_confidence"] == 0.99
    assert category["rejection_reason"] == "no_supporting_evidence"
    assert "Unconfirmed suggestion: goods" in page


def test_category_evidence_does_not_depend_on_accounting_treatment_evidence(
    app: FastAPI,
    client: TestClient,
    seeded_settings: Settings,
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        _meridian(),
        coding={
            "finance_1_purchase_category": ("professional_service", 0.95),
            "finance_1_purchase_category_evidence": ("line", 0.95),
            "finance_1_purchase_category_support": ("supported", 0.95),
            "finance_1_accounting_treatment": ("expense", 0.95),
            "finance_1_accounting_treatment_evidence": ("unknown", 0.99),
        },
    )
    fields = body["lines"][0]["finance_classification"]["fields"]
    assert fields["purchase_category"]["value"] == "professional_service"
    assert fields["accounting_treatment"]["value"] is None
    assert fields["accounting_treatment"]["rejection_reason"] == "no_supporting_evidence"


def test_duplicate_evidence_sources_are_offered_once() -> None:
    from jev_invoice.coding.classification import evidence_choices

    choices = evidence_choices("Legal services", " Legal  services ", "LEGAL SERVICES")
    assert choices == {
        "unknown": "No quotation supports this specific field",
        "line": "Legal services",
    }


def test_evidence_selection_confidence_is_separate_from_support(
    app: FastAPI,
    client: TestClient,
    seeded_settings: Settings,
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        _meridian(),
        coding={
            "finance_1_purchase_category": ("professional_service", 0.99),
            "finance_1_purchase_category_evidence": ("line", 0.36),
            "finance_1_purchase_category_support": ("supported", 0.95),
        },
    )
    field = body["lines"][0]["finance_classification"]["fields"]["purchase_category"]
    assert field["value"] == "professional_service"
    assert field["evidence_confidence"] == 0.36
    assert field["evidence_support_confidence"] == 0.95


def test_high_confidence_but_unsupported_recommendation_is_withheld(
    app: FastAPI,
    client: TestClient,
    seeded_settings: Settings,
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        _meridian(),
        coding={
            "finance_1_purchase_category": ("goods", 0.99),
            "finance_1_purchase_category_evidence": ("line", 0.99),
            "finance_1_purchase_category_support": ("unsupported", 0.95),
        },
    )
    field = body["lines"][0]["finance_classification"]["fields"]["purchase_category"]
    assert field["value"] is None
    assert field["rejection_reason"] == "unsupported_recommendation"


def test_explicit_department_candidate_is_used_without_inventing_a_project(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, server = _process(
        app,
        client,
        seeded_settings,
        _meridian(
            line_items=[{**LEGAL_LINE, "description": "Legal review; Department: Operations"}]
        ),
        coding={
            "finance_1_department": ("candidate_0", 0.97),
            "finance_1_department_evidence": ("line", 0.97),
            "finance_1_department_support": ("supported", 0.97),
        },
    )
    page = client.get(f"/invoices/{body['id']}").text
    assert "Operations" in page
    requests = [r for r in server.requests if "finance_1_project" in r.get("questions", {})]
    assert set(requests[0]["questions"]["finance_1_project"]["criteria"]) == {"unknown"}


def test_po_finance_values_are_inherited_and_not_reclassified(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    vendors = client.get("/purchase-orders").text
    import re

    vendor_id = re.search(r'<option value="([0-9a-f-]+)"[^>]*>Acme Supplies Inc', vendors)
    assert vendor_id is not None
    created = client.post(
        "/purchase-orders",
        data={
            "po_number": "PO-FINANCE",
            "vendor_id": vendor_id.group(1),
            "person": "Sam",
            "issued_date": "2026-09-01",
            "item_description": "Printer paper A4",
            "item_quantity": "10",
            "item_price": "107.40",
            "item_account": "6000",
            "item_purchase_category": "goods",
            "item_department": "Operations",
            "item_project": "Office setup",
            "item_service_dates_required": "false",
            "item_asset_details_required": "false",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    body, server = _process(
        app,
        client,
        seeded_settings,
        acme_invoice_fields(
            po_reference="PO-FINANCE",
            line_items=[
                {
                    "description": "Printer paper A4",
                    "quantity": 10,
                    "unit_price": "107.40",
                    "amount": "1074.00",
                }
            ],
        ),
        coding={"finance_1_conflict": ("conflict", 0.95)},
    )
    page = client.get(f"/invoices/{body['id']}").text
    assert body["lines"][0]["finance_classification"]["fields"]["department"]["source"] == "po"
    assert "FINANCE_CLASSIFICATION_REVIEW" in body["decision"]["reason_codes"]
    assert "Office setup" in page and "Operations" in page and "From PO" in page
    assert "Invoice may conflict with approved PO finance details" in page
    for request in server.requests:
        assert "finance_1_purchase_category" not in request.get("questions", {})
    assert server.coding_requests == []
