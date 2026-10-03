"""The approval limit, edited on the Settings page and used for the next invoice."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
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
    assert "nobody has changed it yet" in page
    assert 'aria-current="page">Settings' in page


def test_saving_a_new_limit_is_shown_and_recorded(client: TestClient) -> None:
    response = _save(client, "$12,500")
    page = client.get(response.headers["location"]).text

    assert response.status_code == 303
    assert "12,500.00" in page
    assert "Set by Maya" in page
    assert "Approval limit changed" in page
    assert "approval limit: 5,000.00 to 12,500.00" in page


@pytest.mark.parametrize(
    ("value", "message"),
    [("abc", "is not an amount"), ("0", "more than zero"), ("-5", "more than zero")],
)
def test_bad_limits_are_refused(client: TestClient, value: str, message: str) -> None:
    response = _save(client, value)

    assert response.status_code == 400
    assert message in response.text
    assert "5,000.00" in client.get("/settings").text  # unchanged


def test_a_name_is_needed(client: TestClient) -> None:
    response = _save(client, "7000", person="")

    assert response.status_code == 400
    assert "Enter your name" in response.text


def test_the_next_invoice_uses_the_new_limit(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    before = _process_acme(app, client, seeded_settings, "AS-40001")
    _save(client, "1000")
    after = _process_acme(app, client, seeded_settings, "AS-40002")

    assert "OVER_APPROVAL_LIMIT" not in before["decision"]["reason_codes"]  # 1,074 < 5,000
    assert "OVER_APPROVAL_LIMIT" in after["decision"]["reason_codes"]  # 1,074 > 1,000
