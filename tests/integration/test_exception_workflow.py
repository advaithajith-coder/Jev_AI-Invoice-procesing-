"""Reviewer workflow regressions use fake models and disposable test databases."""

import re
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from jev_invoice.config import Settings
from jev_invoice.db.models import AuditEvent, InvoiceException
from jev_invoice.db.seed import seed_id
from tests.fakes import acme_invoice_fields
from tests.integration.test_coding import _process
from tests.integration.test_receiving import _deliver, _meridian_twelve_hours
from tests.integration.test_web import _task_id


def _rows(app: FastAPI, client: TestClient, invoice_id: str) -> list[dict[str, Any]]:
    async def fetch() -> list[dict[str, Any]]:
        async with app.state.session_factory() as session:
            rows = await session.scalars(
                select(InvoiceException).where(InvoiceException.invoice_id == uuid.UUID(invoice_id))
            )
            return [
                {
                    "id": str(row.id),
                    "code": row.code,
                    "owner": row.owner,
                    "status": row.status,
                    "assigned_to": row.assigned_to,
                    "reason": row.resolution_reason,
                    "evidence": row.evidence,
                }
                for row in rows
            ]

    assert client.portal is not None
    return client.portal.call(fetch)


def _update(client: TestClient, invoice_id: str, exception_id: str, **fields: str) -> Any:
    return client.post(
        f"/invoices/{invoice_id}/exceptions/{exception_id}",
        data={
            "person": "Maya",
            "owner": "Purchasing",
            "status": "in_progress",
            "assigned_to": "Alex",
            "reason": "Purchasing is confirming the missing order.",
            **fields,
        },
        follow_redirects=False,
    )


def test_owner_status_reason_and_history(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(app, client, seeded_settings, acme_invoice_fields(po_reference=None))
    row = next(row for row in _rows(app, client, body["id"]) if row["code"] == "NO_PURCHASE_ORDER")
    assert row["owner"] == "Purchasing" and row["status"] == "open"
    assert _update(client, body["id"], row["id"]).status_code == 303
    updated = next(r for r in _rows(app, client, body["id"]) if r["id"] == row["id"])
    assert updated["assigned_to"] == "Alex" and updated["status"] == "in_progress"
    assert updated["reason"] == "Purchasing is confirming the missing order."

    async def audit_data() -> list[Any]:
        async with app.state.session_factory() as session:
            return list(
                await session.scalars(
                    select(AuditEvent.data).where(
                        AuditEvent.entity_id == body["id"], AuditEvent.action == "exception.updated"
                    )
                )
            )

    assert client.portal is not None
    history = client.portal.call(audit_data)
    assert (
        history[0]["before"]["status"] == "open" and history[0]["after"]["status"] == "in_progress"
    )
    assert _update(client, body["id"], row["id"], reason="").status_code == 400


def test_recheck_does_not_clear_a_failing_control_or_call_models(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, server = _process(app, client, seeded_settings, acme_invoice_fields(po_reference=None))
    row = next(row for row in _rows(app, client, body["id"]) if row["code"] == "NO_PURCHASE_ORDER")
    calls = len(server.requests)
    response = _update(client, body["id"], row["id"], recheck="yes")
    assert "exception_rechecked" in response.headers["location"]
    assert len(server.requests) == calls
    assert (
        next(r for r in _rows(app, client, body["id"]) if r["id"] == row["id"])["status"] == "open"
    )
    assert "findings unchanged" in client.get(response.headers["location"]).text


def test_delivery_change_shows_before_after_then_clears(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(app, client, seeded_settings, _meridian_twelve_hours())
    row = next(r for r in _rows(app, client, body["id"]) if r["code"] == "QUANTITY_VARIANCE")
    assert row["owner"] == "Delivery team"
    page = client.get(f"/invoices/{body['id']}").text
    assert "Matched PO line" in page and "GRN-5008" in page
    assert "12 billed" in page and "8 received" in page
    assert _deliver(client, "2").status_code == 303
    page = client.get(f"/invoices/{body['id']}").text
    assert "What changed on recheck" in page and "10 received" in page
    after = client.get(f"/api/v1/invoices/{body['id']}").json()
    route = next(
        r for r in after["decision"]["exception_routing"] if r["code"] == "QUANTITY_VARIANCE"
    )
    assert "10 received" in route["reason"] and "8 received" not in route["reason"]
    assert _deliver(client, "2").status_code == 303
    cleared = next(r for r in _rows(app, client, body["id"]) if r["id"] == row["id"])
    assert cleared["status"] == "cleared" and cleared["evidence"]["findings"] == []
    assert "Control no longer reports this issue" in client.get(f"/invoices/{body['id']}").text


def test_review_acceptance_is_not_a_cleared_control(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(app, client, seeded_settings, acme_invoice_fields(po_reference=None))
    page = client.get(f"/invoices/{body['id']}").text
    response = client.post(
        f"/invoices/{body['id']}/review",
        data={
            "task_id": _task_id(page),
            "verdict": "approve",
            "reviewer": "Maya",
            "note": "Confirmed permitted non-PO purchase.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert all(r["status"] == "accepted" for r in _rows(app, client, body["id"]))
    assert "The control was not cleared" in client.get(f"/invoices/{body['id']}").text


def test_security_owner_and_cross_invoice_updates_are_refused(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(
        app,
        client,
        seeded_settings,
        acme_invoice_fields(bank_account="Routing 021000021 Account 9999999999"),
    )
    row = next(r for r in _rows(app, client, body["id"]) if r["code"] == "BANK_ACCOUNT_MISMATCH")
    assert _update(client, body["id"], row["id"], owner="Delivery team").status_code == 400
    assert _update(client, str(uuid.uuid4()), row["id"]).status_code == 400
    assert (
        next(r for r in _rows(app, client, body["id"]) if r["id"] == row["id"])["owner"]
        == "Finance"
    )


def test_matched_delivery_issues_display_beside_the_line(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    assert (
        client.post(
            "/receiving/issues/report",
            data={
                "po_id": str(seed_id("po", "PO-1003")),
                "line_id": str(seed_id("po_line", "PO-1003:2")),
                "quantity": "1",
                "category": "damaged",
                "reason": "Damaged packaging",
                "person": "Sam",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    body, _ = _process(app, client, seeded_settings, acme_invoice_fields())
    page = client.get(f"/invoices/{body['id']}").text
    evidence = page.split('id="evidence-title"', 1)[1].split('id="exception-workflow"', 1)[0]
    assert "Damaged packaging" in evidence and "awaiting credit" in evidence
    assert "GRN-5003" in evidence


def test_tax_cross_check_is_in_line_comparison_without_citations(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    seeded_settings.massachusetts_tax_checks_enabled = True
    body, _ = _process(
        app,
        client,
        seeded_settings,
        acme_invoice_fields(),
        coding={
            "tax_source_1": ("-software-&-digital-goods-", 0.95),
            "tax_treatment_1": ("software", 0.95),
        },
    )
    page = client.get(f"/invoices/{body['id']}").text
    evidence = page.split('id="evidence-title"', 1)[1].split('id="exception-workflow"', 1)[0]
    assert "Tax recommendation" in evidence
    assert "Supporting source passage" not in evidence
    assert "https://www.mass.gov/" not in evidence
    assert re.search(r"Tax recommendation.*6.25%", evidence)
