"""Invoices in another currency: converted to USD at the rate for their date, then checked."""

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from jev_invoice.config import Settings
from jev_invoice.db.models import ExchangeRate, Vendor
from jev_invoice.db.session import create_engine, create_session_factory
from jev_invoice.decision.service import DecisionService
from jev_invoice.extraction.client import LLMClient
from jev_invoice.extraction.service import ExtractionService
from jev_invoice.fx.service import FrankfurterSource
from tests.fakes import FakeJevServer, FakeLLMProvider, make_jev_client, wire_answers

STRATUS_BANK = "Routing 071000013 Account 1234567890"  # matches seed data
INR_RATE = Decimal("0.01038")


class FakeRates:
    name = "test rates"

    def __init__(self, rate: Decimal | None = INR_RATE) -> None:
        self.rate = rate
        self.calls: list[tuple[str, str, date]] = []

    async def fetch(self, currency: str, base: str, on: date) -> tuple[Decimal, date] | None:
        self.calls.append((currency, base, on))
        return None if self.rate is None else (self.rate, on)


# Stratus bills without a PO: a budget owner signs off and Finance confirms the coding.
NO_PO_SIGN_OFF = {"NON_PO_APPROVAL_REQUIRED", "GL_CODING_REVIEW"}


def _stratus_inr(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "vendor_name": "Stratus Telecom Inc",
        "invoice_number": "ST-IN-1001",
        "invoice_date": "2026-10-02",
        "due_date": "2026-11-01",
        "currency": "INR",
        "bill_to_name": "Northwind Analytics Inc",
        "bank_account": STRATUS_BANK,
        "subtotal": 300000,
        "tax": 0,
        "total": 300000,
        "line_items": [
            {"description": "Mumbai office fiber", "quantity": 1, "unit_price": 300000,
             "amount": 300000},
        ],
    }  # fmt: skip
    fields.update(overrides)
    return fields


def _run(settings: Settings, statement: Any) -> Any:
    async def go() -> Any:
        engine = create_engine(settings)
        try:
            async with create_session_factory(engine)() as session, session.begin():
                return await session.execute(statement)
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def inr_vendor(seeded_settings: Settings) -> None:
    """Stratus bills the company's Indian office in rupees."""
    _run(
        seeded_settings,
        update(Vendor).where(Vendor.name == "Stratus Telecom Inc").values(default_currency="INR"),
    )


def _upload(
    client: TestClient, app: FastAPI, settings: Settings, fields: dict[str, Any], rates: Any
) -> dict[str, Any]:
    app.state.extraction = ExtractionService(LLMClient(FakeLLMProvider(fields), settings))
    app.state.decisions = DecisionService(
        make_jev_client(FakeJevServer(wire_answers()), settings), settings
    )
    app.state.rates = rates
    name = f"{fields['invoice_number']}.txt"
    response = client.post("/api/v1/invoices", files={"file": (name, name.encode(), "text/plain")})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.usefixtures("inr_vendor")
def test_inr_invoice_is_converted_and_treated_like_any_other(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    # INR 300,000 is USD 3,114.00 at 0.01038: under the USD 5,000 limit. Stratus bills
    # without a PO, so like any Stratus bill it needs a budget owner's sign-off; nothing
    # about the currency holds it.
    body = _upload(client, app, seeded_settings, _stratus_inr(), FakeRates())

    assert set(body["decision"]["reason_codes"]) == NO_PO_SIGN_OFF
    [task] = client.get("/api/v1/review").json()
    client.post(f"/api/v1/review/{task['id']}/approve", json={"reviewer": "maya"})
    page = client.get(f"/invoices/{body['id']}").text
    assert "≈ USD 3,114.00" in page
    assert "1 INR = 0.01038 USD" in page
    payables = client.get("/payables").text
    assert "INR 300,000.00" in payables
    assert "USD 3,114.00" in payables  # the total is in dollars


@pytest.mark.usefixtures("inr_vendor")
def test_approval_limit_compares_the_converted_total(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    # INR 600,000 = USD 6,228.00: over the limit.
    fields = _stratus_inr(subtotal=600000, total=600000, line_items=[])
    body = _upload(client, app, seeded_settings, fields, FakeRates())

    codes = set(body["decision"]["reason_codes"])
    assert "OVER_APPROVAL_LIMIT" in codes
    assert codes - NO_PO_SIGN_OFF == {"OVER_APPROVAL_LIMIT"}
    messages = [finding["message"] for finding in body["decision"]["findings"]]
    assert any("USD 6,228.00" in message for message in messages)


@pytest.mark.usefixtures("inr_vendor")
def test_without_a_rate_a_person_decides(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    body = _upload(client, app, seeded_settings, _stratus_inr(), FakeRates(rate=None))

    assert body["decision"]["action"] == "hold_review"
    # Rupees are never compared with the dollar limit as if they were dollars.
    assert set(body["decision"]["reason_codes"]) - NO_PO_SIGN_OFF == {"FOREIGN_CURRENCY"}


@pytest.mark.usefixtures("inr_vendor")
def test_each_rate_is_fetched_once(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    rates = FakeRates()
    _upload(client, app, seeded_settings, _stratus_inr(), rates)
    _upload(client, app, seeded_settings, _stratus_inr(invoice_number="ST-IN-1002"), rates)

    assert rates.calls == [("INR", "USD", date(2026, 10, 2))]
    stored = _run(seeded_settings, select(func.count()).select_from(ExchangeRate)).scalar()
    assert stored == 1


def test_a_vendor_that_bills_in_dollars_still_needs_a_person_for_rupees(
    client: TestClient, app: FastAPI, seeded_settings: Settings
) -> None:
    body = _upload(client, app, seeded_settings, _stratus_inr(), FakeRates())

    assert set(body["decision"]["reason_codes"]) - NO_PO_SIGN_OFF == {"CURRENCY_MISMATCH"}


# ── The ECB rate source ───────────────────────────────────────────────────────


async def test_frankfurter_reads_the_rate_and_its_publication_date() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/2026-10-04"
        assert request.url.params["base"] == "INR"
        assert request.url.params["symbols"] == "USD"
        # A Sunday: the ECB's last rate is Friday's.
        return httpx.Response(
            200,
            json={"amount": 1.0, "base": "INR", "date": "2026-10-02", "rates": {"USD": 0.01038}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        source = FrankfurterSource(http, "https://rates.test/v1")
        assert await source.fetch("INR", "USD", date(2026, 10, 4)) == (
            INR_RATE,
            date(2026, 10, 2),
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"message": "not found"}),
        httpx.Response(200, json={"rates": {}}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_frankfurter_failures_mean_no_rate(response: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        source = FrankfurterSource(http, "https://rates.test/v1")
        assert await source.fetch("INR", "USD", date(2026, 10, 2)) is None
