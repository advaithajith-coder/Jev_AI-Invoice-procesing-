"""Cutoff, accounting treatment and PO tax use temporary databases and fake models."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.accounting.controls import AccountingPolicy, check_accounting, save_policy
from jev_invoice.config import Settings
from jev_invoice.db.models import Invoice, InvoiceLine, PurchaseOrder, PurchaseOrderLine
from jev_invoice.domain.enums import ReasonCode
from jev_invoice.masterdata.purchase_orders import POLineIn
from tests.fakes import acme_invoice_fields
from tests.integration.test_coding import _process


def test_po_tax_groups_rates_and_leaves_missing_tax_unknown() -> None:
    po = PurchaseOrder(
        total_amount=Decimal("500.16"),
        lines=[
            PurchaseOrderLine(
                quantity=Decimal("2"),
                unit_price=Decimal("0.08"),
                finance_details={"tax_rate_percent": "6.25"},
            ),
            PurchaseOrderLine(
                quantity=Decimal("1"),
                unit_price=Decimal("500"),
                finance_details={"tax_rate_percent": "0"},
            ),
        ],
    )
    assert po.tax_amount == Decimal("0.01")
    assert po.grand_total == Decimal("500.17")
    po.lines[1].finance_details = None
    assert po.tax_amount is None and po.grand_total is None


@pytest.mark.parametrize("rate", ["Infinity", "NaN", "-1", "100.01"])
def test_invalid_po_tax_rates_are_rejected(rate: str) -> None:
    with pytest.raises(ValidationError):
        POLineIn(
            description="Goods",
            quantity=Decimal("1"),
            unit_price=Decimal("10"),
            tax_rate_percent=Decimal(rate),
        )


def invoice_line(**values: Any) -> InvoiceLine:
    return InvoiceLine(
        line_no=1,
        description="Annual subscription",
        quantity=Decimal("1"),
        unit_price=Decimal("1200"),
        amount=Decimal("1200"),
        finance_classification={
            "fields": {
                "purchase_category": {"value": "subscription"},
                "service_start": {"value": "2026-01-01"},
                "service_end": {"value": "2026-12-31"},
            }
        },
        **values,
    )


async def test_annual_subscription_requires_treatment_review(
    session: AsyncSession,
) -> None:
    await save_policy(session, AccountingPolicy(), "Finance")
    invoice = Invoice(invoice_date=date(2026, 9, 10), currency="USD", lines=[invoice_line()])
    findings = await check_accounting(session, invoice)
    assert {f.code for f in findings} == {
        ReasonCode.ACCOUNTING_TREATMENT_REVIEW,
    }
    assert invoice.additional_info is not None
    state = invoice.additional_info["accounting"]
    assert state["posting_period"] == "2026-09"
    assert state["lines"][0]["months"] == 12
    state.update(
        posting_period="2026-10",
        posting_reason="Finance approved cutoff adjustment",
    )
    state["resolutions"] = {
        "1": {
            "reason": "Allocate over service term",
            "treatment": "prepayment",
            "fingerprint": state["lines"][0]["fingerprint"],
        }
    }
    assert await check_accounting(session, invoice) == []
    assert invoice.invoice_date == date(2026, 9, 10)


async def test_asset_threshold_is_per_unit_and_requires_company_policy(
    session: AsyncSession,
) -> None:
    await save_policy(
        session,
        AccountingPolicy(asset_threshold=Decimal("1000"), policy_reference="Approved asset policy"),
        "Finance",
    )
    line = invoice_line()
    line.description = "Laptops"
    line.quantity, line.unit_price, line.amount = Decimal("3"), Decimal("900"), Decimal("2700")
    line.finance_classification = {"fields": {"asset_details_required": {"value": True}}}
    invoice = Invoice(invoice_date=date(2026, 10, 1), currency="USD", lines=[line])
    assert await check_accounting(session, invoice) == []
    line.unit_price = Decimal("1500")
    assert ReasonCode.ACCOUNTING_TREATMENT_REVIEW in {
        f.code for f in await check_accounting(session, invoice)
    }


def test_accounting_form_preserves_invoice_date(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    response = client.post(
        "/settings/accounting-policy",
        data={
            "person": "Maya",
            "closed_through": "2026-09",
            "prepayment_rule": "annual",
            "policy_reference": "September close approved",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    body, _ = _process(app, client, seeded_settings, acme_invoice_fields(invoice_date="2026-09-10"))
    invoice_id = body["id"]
    assert body["status"] == "approved"
    saved = client.post(
        f"/invoices/{invoice_id}/accounting",
        data={
            "person": "Maya",
            "reason": "Late invoice, approved October posting",
            "posting_period": "2026-10",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303, saved.text
    page = client.get(f"/invoices/{invoice_id}").text
    assert 'value="2026-10"' in page
    detail = client.get(f"/api/v1/invoices/{invoice_id}").json()
    assert detail["invoice_date"] == "2026-09-10"


def test_export_preserves_separate_posting_period(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    body, _ = _process(app, client, seeded_settings, acme_invoice_fields(invoice_date="2026-09-10"))
    assert body["status"] == "approved"
    client.post(
        "/settings/accounting-policy",
        data={
            "person": "Maya",
            "closed_through": "2026-09",
            "prepayment_rule": "annual",
            "policy_reference": "Month closed",
        },
    )
    saved = client.post(
        f"/invoices/{body['id']}/accounting",
        data={
            "person": "Maya",
            "reason": "Late invoice approved in October",
            "posting_period": "2026-10",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    unsupported = client.post(
        "/api/v1/exports",
        json={"format": "xero", "invoice_ids": [body["id"]], "created_by": "Maya"},
    )
    assert unsupported.status_code == 422 and "separate posting-period" in unsupported.text
    exported = client.post(
        "/api/v1/exports",
        json={"format": "standard", "invoice_ids": [body["id"]], "created_by": "Maya"},
    )
    assert exported.status_code == 201, exported.text
    content = client.get(f"/api/v1/exports/{exported.json()['id']}/file").text
    assert "posting_period" in content and "2026-10" in content and "2026-09-10" in content


def test_po_api_exposes_net_tax_and_gross(client: TestClient) -> None:
    vendors = client.get("/api/v1/vendors").json()
    vendor = next(v for v in vendors if v["name"] == "Acme Supplies Inc")
    response = client.post(
        "/api/v1/purchase-orders",
        json={
            "po_number": "PO-TAX-TEST",
            "vendor_id": vendor["id"],
            "issued_date": "2026-10-01",
            "currency": "USD",
            "created_by": "Maya",
            "lines": [
                {
                    "description": "Supplies",
                    "quantity": "2",
                    "unit_price": "100",
                    "tax_rate_percent": "6.25",
                },
                {
                    "description": "Pure consulting",
                    "quantity": "1",
                    "unit_price": "500",
                    "tax_rate_percent": "0",
                },
            ],
        },
    )
    assert response.status_code == 201, response.text
    po = response.json()
    assert Decimal(po["subtotal_amount"]) == Decimal("700")
    assert Decimal(po["tax_amount"]) == Decimal("12.50")
    assert Decimal(po["grand_total"]) == Decimal("712.50")


async def test_multi_month_review_is_fixed_including_previous_disabled_setting(
    session: AsyncSession,
) -> None:
    from jev_invoice.accounting.controls import load_policy
    from jev_invoice.db.models import AppSetting

    session.add(AppSetting(key="accounting_prepayment_rule", value="off", updated_by="Finance"))
    await session.commit()
    assert (await load_policy(session)).prepayment_rule == "multi_period"
    line = invoice_line()
    line.description = "Quarterly subscription"
    assert line.finance_classification is not None
    line.finance_classification["fields"]["service_end"]["value"] = "2026-03-31"
    invoice = Invoice(invoice_date=date(2026, 1, 1), currency="USD", lines=[line])
    assert ReasonCode.ACCOUNTING_TREATMENT_REVIEW in {
        f.code for f in await check_accounting(session, invoice)
    }
    line.finance_classification["fields"]["service_end"]["value"] = "2026-01-31"
    assert await check_accounting(session, invoice) == []


def test_settings_has_fixed_subscription_review_without_selector(client: TestClient) -> None:
    page = client.get("/settings").text
    assert "Finance reviews subscriptions spanning multiple months" in page
    assert 'name="prepayment_rule"' not in page
    response = client.post(
        "/settings/accounting-policy",
        data={"person": "Maya", "prepayment_rule": "off"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Finance reviews subscriptions spanning multiple months" in client.get("/settings").text


async def test_previous_closed_month_is_ignored(session: AsyncSession) -> None:
    from jev_invoice.db.models import AppSetting

    session.add(AppSetting(key="accounting_closed_through", value="2026-09", updated_by="Finance"))
    await session.commit()
    invoice = Invoice(invoice_date=date(2026, 9, 10), currency="USD", lines=[])
    assert await check_accounting(session, invoice) == []


def test_settings_has_no_closed_month_field(client: TestClient) -> None:
    assert 'name="closed_through"' not in client.get("/settings").text


async def test_demo_defaults_and_disabled_threshold_persist_without_history(
    session: AsyncSession,
) -> None:
    from sqlalchemy import select

    from jev_invoice.accounting.controls import load_policy
    from jev_invoice.db.models import AuditEvent

    policy = await load_policy(session)
    assert policy.asset_threshold == Decimal("1000.00")
    assert "Demo policies" in policy.policy_reference
    await save_policy(
        session,
        AccountingPolicy(asset_threshold=None, policy_reference="Demo: threshold disabled"),
        "Maya",
    )
    assert (await load_policy(session)).asset_threshold is None
    assert (
        list(await session.scalars(select(AuditEvent).where(AuditEvent.entity_type == "setting")))
        == []
    )


def test_settings_shows_demo_policies_without_history(client: TestClient) -> None:
    page = client.get("/settings").text
    assert "Company policies" in page and "Northwind Analytics policies" in page
    assert "1,000.00" in page
    assert 'action="/settings/accounting-policy"' not in page
    assert 'name="policy_reference"' not in page
    assert 'id="history-title"' not in page
    response = client.post(
        "/settings/accounting-policy",
        data={
            "person": "Maya",
            "asset_threshold": "1500",
            "policy_reference": "Demo equipment policy",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "1,500.00" in client.get("/settings").text
