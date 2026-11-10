from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from tests.fakes import acme_invoice_fields
from tests.integration.test_coding import _process


def test_supplier_route_names_internal_owner_and_preserves_review(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        acme_invoice_fields(po_reference=None),
        coding={"route_0": ("supplier", 0.96), "owner_0": ("purchasing", 0.95)},
    )
    route = body["decision"]["exception_routing"][0]
    assert route["route"] == "Supplier clarification needed"
    assert route["owner"] == "Purchasing"
    assert "supplier" in route["next_action"]
    assert route["source"] == "jev"
    assert body["status"] == "pending_review"
    page = client.get(f"/invoices/{body['id']}").text
    assert "Needs attention" in page and "Responsible team" in page
    assert "Who should investigate" not in page
    assert "Supplier clarification needed" in page and "Next action" in page
    assert "Purchasing" in client.get("/review").text


def test_low_confidence_routing_uses_labelled_fallback(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        acme_invoice_fields(po_reference=None),
        coding={"route_0": ("delivery", 0.2)},
    )
    route = body["decision"]["exception_routing"][0]
    assert route["source"] == "rule_fallback"
    assert route["owner"] == "Purchasing"
    assert route["evidence"]


def test_security_exception_cannot_be_routed_away_from_finance(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        acme_invoice_fields(bank_account="Routing 021000021 Account 9999999999"),
        coding={"route_0": ("delivery", 0.98)},
    )
    route = next(
        r for r in body["decision"]["exception_routing"] if r["code"] == "BANK_ACCOUNT_MISMATCH"
    )
    assert route["owner"] == "Finance" and route["route"] == "Finance"
    assert route["source"] == "security_rule"
    assert "payment-security" in route["next_action"]


def test_clear_invoice_does_not_request_exception_routing(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, server = _process(app, client, seeded_settings, acme_invoice_fields())
    assert body["decision"]["exception_routing"] == []
    assert not any("route_0" in r.get("questions", {}) for r in server.requests)
