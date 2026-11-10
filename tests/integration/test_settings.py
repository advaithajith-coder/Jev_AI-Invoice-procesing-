"""The approval limit, edited on the Settings page and used for the next invoice."""

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from jev_invoice.config import Settings
from jev_invoice.db.models import AppSetting
from jev_invoice.db.session import create_engine, create_session_factory
from tests.fakes import acme_invoice_fields, install_fakes, wire_answers


def _save(client: TestClient, value: str, person: str = "Maya") -> Any:
    return client.post(
        "/settings/approval-limit",
        data={"approval_limit": value, "person": person},
        follow_redirects=False,
    )


def _process_acme(app: FastAPI, client: TestClient, settings: Settings, number: str) -> Any:
    """Acme's clean 1,074.00 invoice against PO-1003."""
    install_fakes(app, settings, acme_invoice_fields(invoice_number=number), jev=[wire_answers()])
    return client.post(
        "/api/v1/invoices", files={"file": (f"{number}.txt", number.encode(), "text/plain")}
    ).json()


def test_page_shows_the_configured_default(client: TestClient) -> None:
    page = client.get("/settings").text

    assert "5,000.00" in page
    assert "Company policies" in page
    assert 'aria-current="page">Company policies' in page


def test_saving_a_new_limit_is_shown_and_recorded(
    client: TestClient, seeded_settings: Settings
) -> None:
    response = _save(client, "$12,500")
    page = client.get(response.headers["location"]).text

    assert response.status_code == 303
    assert "12,500.00" in page
    assert "Saved. Invoices processed from now on use the new limit." in page
    assert "Set by Maya" not in page  # Policy history was removed from this screen.

    async def saved_actor() -> bool:
        engine = create_engine(seeded_settings)
        try:
            async with create_session_factory(engine)() as session:
                return (
                    await session.scalar(
                        select(AppSetting.key).where(
                            AppSetting.updated_by == "Maya", AppSetting.value == "12500.00"
                        )
                    )
                    is not None
                )
        finally:
            await engine.dispose()

    assert asyncio.run(saved_actor())


@pytest.mark.parametrize(
    ("value", "message"),
    [("abc", "is not an amount"), ("0", "more than zero"), ("-5", "more than zero")],
)
def test_bad_limits_are_refused(client: TestClient, value: str, message: str) -> None:
    response = _save(client, value)

    assert response.status_code == 400
    assert message in response.text
    assert "5,000.00" in client.get("/settings").text  # unchanged


def test_limit_can_be_saved_without_a_name(client: TestClient) -> None:
    assert 'name="person"' not in client.get("/settings").text
    response = client.post(
        "/settings/approval-limit", data={"approval_limit": "7000"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "7,000.00" in client.get(response.headers["location"]).text


def test_the_next_invoice_uses_the_new_limit(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    before = _process_acme(app, client, seeded_settings, "AS-40001")
    _save(client, "1000")
    after = _process_acme(app, client, seeded_settings, "AS-40002")

    assert "OVER_APPROVAL_LIMIT" not in before["decision"]["reason_codes"]  # 1,074 < 5,000
    assert "OVER_APPROVAL_LIMIT" in after["decision"]["reason_codes"]  # 1,074 > 1,000
