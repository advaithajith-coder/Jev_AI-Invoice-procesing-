from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from jev_invoice.db.models import Invoice, InvoiceLine
from jev_invoice.tax.massachusetts import (
    SOURCE_KEYS,
    _exact,
    check_tax,
    citation,
    rule_catalog,
)
from jev_invoice.web.review_evidence import tax_summary
from tests.fakes import FakeJevServer, acme_invoice_fields, make_jev_client
from tests.integration.test_coding import _process


def invoice(description: str, tax: str) -> Invoice:
    return Invoice(
        currency="USD",
        invoice_date=date(2026, 10, 5),
        subtotal=Decimal("100"),
        tax_total=Decimal(tax),
        discount=Decimal("0"),
        shipping=Decimal("0"),
        lines=[InvoiceLine(line_no=1, description=description, amount=Decimal("100"))],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description", "tax", "passes"),
    [
        ("Legal services, October 2026", "0", True),
        ("Legal services, October 2026", "6.25", False),
        ("Printer paper", "6.25", True),
        ("Printer paper", "0", False),
        ("Legal services and laptop", "0", False),
        ("Consulting services", "0", False),
        ("SaaS hosting bundle", "6.25", False),
    ],
)
async def test_tax_rule_cases(description: str, tax: str, passes: bool) -> None:
    item = invoice(description, tax)
    findings = await check_tax(item, rule_only=True)
    assert (not findings) is passes
    assert item.additional_info is not None
    for row in item.additional_info["tax_review"]["lines"]:
        assert "source" not in row and "source_confidence" not in row


@pytest.mark.asyncio
async def test_unknown_tax_and_adjustments() -> None:
    item = invoice("Printer paper", "6.25")
    item.tax_total = None
    item.shipping = Decimal("5")
    findings = await check_tax(item)
    assert any("missing" in f.message for f in findings)
    assert any("shipping" in f.message for f in findings)


def test_catalog_exemption_evidence() -> None:
    rule = next(r for r in rule_catalog() if r["category"] == "Exempt organizations")
    assert "ST-2" in rule["treatment"] and "ST-5" in rule["treatment"]
    assert "url" not in rule and "passage" not in rule


def test_pipeline_routes_tax_to_finance(
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
    assert body["status"] == "pending_review"
    route = next(
        r for r in body["decision"]["exception_routing"] if r["code"] == "TAX_REVIEW_REQUIRED"
    )
    assert route["owner"] == "Finance" and route["source"] == "tax_rule"
    page = client.get(f"/invoices/{body['id']}").text
    assert "Massachusetts tax verification" in page
    assert "Expected tax" in page
    assert "Taxable subtotal" in page and "Supplier tax" in page and "Difference" in page
    assert "Source selection:" not in page
    assert "https://www.mass.gov/guides/sales-and-use-tax#" not in page
    rules = client.get("/tax-rules")
    assert rules.status_code == 200
    assert "Office and commercial premises rent" in rules.text
    assert "Rate and amount cross-checks" in rules.text
    assert "830-CMR-64h13-computer-industry-services-and-products" not in rules.text


@pytest.mark.parametrize(
    ("description", "treatment", "source", "confidence", "tax", "passes"),
    [
        (
            "Contract review fees",
            "professional",
            "personal-or-professional-services",
            0.95,
            "0",
            True,
        ),
        (
            "Remote standardized analytics software licence",
            "software",
            "-software-&-digital-goods-",
            0.95,
            "6.25",
            True,
        ),
        (
            "Contract review fees",
            "professional",
            "personal-or-professional-services",
            0.60,
            "0",
            False,
        ),
        ("Printer paper", "professional", "personal-or-professional-services", 0.95, "0", False),
        ("Contract review fees", "professional", "-software-&-digital-goods-", 0.95, "0", True),
        (
            "Office premises rent, Suite 400, October 2026",
            "rent",
            "real-property-rent",
            0.95,
            "0",
            True,
        ),
        (
            "Office premises rent, Suite 400, October 2026",
            "rent",
            "computer-services",
            0.95,
            "0",
            True,
        ),
        ("Managed hosting, no software access", "hosting", "computer-services", 0.95, "0", True),
        ("Business telephone voice service", "telephone", "ignored", 0.95, "5", False),
        ("Exempt equipment purchase", "exemption", "ignored", 0.95, "0", False),
        ("IT consultancy, network review", "professional", "computer-services", 0.95, "0", True),
        (
            "Remote analytics platform access",
            "software",
            "computer-software-rules",
            0.95,
            "6.25",
            True,
        ),
    ],
)
def test_jev_understands_new_wording_without_citation_checks(
    app: FastAPI,
    client: TestClient,
    seeded_settings: Settings,
    description: str,
    treatment: str,
    source: str,
    confidence: float,
    tax: str,
    passes: bool,
) -> None:
    seeded_settings.massachusetts_tax_checks_enabled = True
    total = str(Decimal("100") + Decimal(tax))
    fields = acme_invoice_fields(
        subtotal="100",
        tax=tax,
        total=total,
        amount_due=total,
        discount="0",
        shipping="0",
        amount_paid="0",
        line_items=[
            {"description": description, "quantity": None, "unit_price": None, "amount": "100"}
        ],
    )
    body, server = _process(
        app,
        client,
        seeded_settings,
        fields,
        coding={"tax_treatment_1": (treatment, confidence), "tax_source_1": (source, 0.95)},
    )
    reasons = body["decision"]["reason_codes"]
    assert ("TAX_REVIEW_REQUIRED" not in reasons) is passes
    if _exact(description)[1] is None:
        assert any("tax_treatment_1" in r.get("questions", {}) for r in server.requests)
    assert not any(
        q.startswith("tax_source_") for r in server.requests for q in r.get("questions", {})
    )


@pytest.mark.asyncio
async def test_unavailable_model_and_changed_evidence_require_review() -> None:
    item = invoice("Remote software access", "6.25")
    assert await check_tax(item)
    # A failed assessment must never turn into a successful cached assessment.
    assert await check_tax(item)
    assert item.additional_info is not None
    assert item.additional_info["tax_review"]["model_status"] == "unavailable"
    jev = make_jev_client(
        FakeJevServer(coding={"tax_treatment_1": ("software", 0.95)}), Settings(_env_file=None)
    )
    try:
        assert not await check_tax(item, jev)
    finally:
        await jev.aclose()
    assert not await check_tax(item)
    item.lines[0].description = "Unspecified bundle"
    assert await check_tax(item)


@pytest.mark.asyncio
async def test_source_confidence_does_not_block_a_rate_cross_check() -> None:
    item = invoice("Contract review fees", "0")
    jev = make_jev_client(
        FakeJevServer(
            coding={
                "tax_treatment_1": ("professional", 0.95),
                "tax_source_1": ("personal-or-professional-services", 0.70),
            }
        ),
        Settings(_env_file=None),
    )
    try:
        findings = await check_tax(item, jev)
    finally:
        await jev.aclose()
    assert not findings
    assert item.additional_info is not None
    row = item.additional_info["tax_review"]["lines"][0]
    assert row["expected_rate"] == "0"
    assert row["rejection_reasons"] == []
    assert "source_choice" not in row and "source_confidence" not in row


@pytest.mark.asyncio
async def test_partial_tax_is_not_invoice_wide_tax() -> None:
    item = invoice("Printer paper", "6.25")
    item.subtotal = Decimal("300")
    item.lines.append(InvoiceLine(line_no=2, description="Unclear bundle", amount=Decimal("200")))
    jev = make_jev_client(
        FakeJevServer(
            coding={
                "tax_treatment_1": ("goods", 0.95),
                "tax_source_1": ("sales-tax", 0.95),
                "tax_treatment_2": ("unknown", 0.95),
                "tax_source_2": ("unknown", 0.95),
            }
        ),
        Settings(_env_file=None),
    )
    try:
        assert await check_tax(item, jev)
    finally:
        await jev.aclose()
    assert item.additional_info is not None
    review = item.additional_info["tax_review"]
    assert review["expected_tax"] is None
    assert review["accepted_lines_tax"] == "6.25"
    assert review["accepted_line_count"] == review["unresolved_line_count"] == 1


@pytest.mark.parametrize(
    ("description", "section"),
    [
        ("Office rent, October 2026", "real-property-rent"),
        ("Warehouse lease, Unit 7", "real-property-rent"),
        ("Copier lease, October 2026", "sales-tax"),
        ("Hotel room rent, Boston", "sales-tax"),
        ("Managed hosting, October 2026", "computer-services"),
        ("IT consulting, network review", "computer-services"),
        ("Analytics software subscription", "-software-&-digital-goods-"),
    ],
)
def test_description_points_to_the_right_source(description: str, section: str) -> None:
    # Only a hint for the reviewer and Jev: no rate is set from these keywords.
    assert _exact(description) == (section, None)


def test_offline_reference_data_is_preserved() -> None:
    for key in ("real-property-rent", "computer-software-rules", "computer-services"):
        assert key in SOURCE_KEYS
        cited = citation(key)
        assert cited["passage"]
        assert cited["url"].startswith(
            ("https://www.mass.gov/regulations/830-CMR-64h13", "https://malegislature.gov/")
        )


@pytest.mark.asyncio
async def test_exact_rules_work_without_a_model_and_mixed_unit_tax_is_correct() -> None:
    item = invoice("Printer paper", "6.25")
    item.subtotal = Decimal("200")
    item.lines[0].quantity = Decimal("10")
    item.lines[0].unit_price = Decimal("10")
    item.lines.append(InvoiceLine(line_no=2, description="Legal services", amount=Decimal("100")))
    assert not await check_tax(item)
    assert item.additional_info is not None
    review = item.additional_info["tax_review"]
    assert review["expected_tax"] == "6.25"  # Taxable line amount already includes quantity.
    assert tax_summary(item) == {"taxable_subtotal": Decimal(100), "difference": Decimal(0)}
    assert review["model_status"] == "not_needed"
    assert [r["expected_rate"] for r in review["lines"]] == ["6.25", "0"]
    item.tax_total = Decimal("62.50")
    assert await check_tax(item)  # Do not multiply quantity a second time or skip amount checks.
    assert tax_summary(item)["difference"] == Decimal("56.25")
    item.shipping = Decimal(5)
    assert await check_tax(item)
    assert tax_summary(item) == {"taxable_subtotal": None, "difference": None}
