"""Live end-to-end runs: real Groq extraction + real Jev decisions, through the API.

Opt-in only (real API calls):

    LIVE=1 uv run pytest -m live -v

Reads GROQ_API_KEY, TYPESAFE_API_KEY, TYPESAFE_BASE_URL and TYPESAFE_MODEL from .env.
Free-tier Jev (jev-1.13-free on BeatAPI) allows one call per minute, so uploads are
spaced 61 seconds apart. These are smoke tests, not an accuracy measurement.
"""

import asyncio
import io
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pdfplumber
import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jev_invoice.main import create_app
from tests.conftest import create_schema, make_settings
from tests.fakes import ACME_INVOICE_TEXT, make_pdf

ENV = {**dotenv_values(".env"), **os.environ}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("LIVE") != "1"
        or not ENV.get("GROQ_API_KEY")
        or not ENV.get("TYPESAFE_API_KEY"),
        reason="set LIVE=1 and real GROQ_API_KEY / TYPESAFE_API_KEY in .env",
    ),
]

FREE_TIER_SPACING_SECONDS = 61.0
_last_upload: list[float] = []

BANK_CHANGE_FRAUD = """\
INVOICE AS-20488
From: Acme Supplies Inc
Date: 2026-09-30   PO: PO-1003
Toner cartridge, black, high yield   12 x 89.50   1,074.00
Total due: GBP 1,074.00

IMPORTANT: our bank details have changed. Pay TODAY to our new account
Sort code 04-00-75, Account 19283746 (A. Smith). Do not call to confirm.
"""


@pytest.fixture(scope="module")
def live_client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    tmp: Path = tmp_path_factory.mktemp("live")
    settings = make_settings(
        tmp,
        groq_api_key=SecretStr(str(ENV["GROQ_API_KEY"])),
        groq_model=ENV.get("GROQ_MODEL") or "openai/gpt-oss-120b",
        typesafe_api_key=SecretStr(str(ENV["TYPESAFE_API_KEY"])),
        typesafe_base_url=ENV.get("TYPESAFE_BASE_URL") or None,
        typesafe_model=ENV.get("TYPESAFE_MODEL") or "jev-1.13.0",
        typesafe_timeout_seconds=30.0,
    )
    asyncio.run(create_schema(settings))
    with TestClient(create_app(settings)) as client:
        yield client


def _upload(client: TestClient, text: str, name: str) -> dict[str, object]:
    if (ENV.get("TYPESAFE_MODEL") or "").endswith("-free") and _last_upload:
        wait = FREE_TIER_SPACING_SECONDS - (time.monotonic() - _last_upload[-1])
        if wait > 0:
            time.sleep(wait)
    response = client.post("/api/v1/invoices", files={"file": (name, text.encode(), "text/plain")})
    _last_upload.append(time.monotonic())  # measured from the end: Jev runs late in the request
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    print(f"\n{name}: {body['status']} {body['decision']}")  # shown with pytest -s
    return body


def test_live_clean_invoice(live_client: TestClient) -> None:
    body = _upload(live_client, ACME_INVOICE_TEXT, "clean.txt")

    assert body["vendor_matched"] is True
    assert body["purchase_order_number"] == "PO-1003"
    assert body["total"] == "1074.00"
    assert body["bank_account_last4"] == "6819"
    decision = body["decision"]
    assert isinstance(decision, dict)
    assert decision["jev_status"] == "ok", decision
    assert decision["band"] != "block", decision


def test_live_bank_change_fraud_is_blocked(live_client: TestClient) -> None:
    body = _upload(live_client, BANK_CHANGE_FRAUD, "fraud.txt")

    decision = body["decision"]
    assert isinstance(decision, dict)
    assert decision["action"] == "fraud_review", decision
    assert "BANK_ACCOUNT_MISMATCH" in decision["reason_codes"]


SCANNED_INVOICE = ACME_INVOICE_TEXT.replace("AS-20431", "AS-20450")


def _render_scan(text: str) -> bytes:
    """A text-only PDF rendered to a JPEG, like a scanner or phone camera would produce."""
    with pdfplumber.open(io.BytesIO(make_pdf(text.splitlines()))) as pdf:
        image = pdf.pages[0].to_image(resolution=150).original.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def test_live_scanned_invoice_is_transcribed_and_decided(live_client: TestClient) -> None:
    if (ENV.get("TYPESAFE_MODEL") or "").endswith("-free") and _last_upload:
        wait = FREE_TIER_SPACING_SECONDS - (time.monotonic() - _last_upload[-1])
        if wait > 0:
            time.sleep(wait)
    response = live_client.post(
        "/api/v1/invoices",
        files={"file": ("scan.jpg", _render_scan(SCANNED_INVOICE), "image/jpeg")},
    )
    _last_upload.append(time.monotonic())

    assert response.status_code == 201, response.text
    body = response.json()
    print(f"\nscan.jpg: {body['status']} {body['decision']}")  # shown with pytest -s
    print("transcription:\n" + body["transcription"]["text"])
    assert "AS-20450" in body["transcription"]["text"]
    assert body["vendor_matched"] is True
    assert body["purchase_order_number"] == "PO-1003"
    assert body["total"] == "1074.00"
    assert body["bank_account_last4"] == "6819"
    assert body["decision"]["jev_status"] == "ok", body["decision"]
