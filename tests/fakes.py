"""Test doubles: a scripted LLM provider, a fake Jev server, and document builders.

The Jev fake runs the real TypeSafe SDK on an in-memory httpx2 transport, so tests
exercise the SDK's request encoding and response decoding without network.
"""

import io
import json
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx2
from fastapi import FastAPI
from PIL import Image, ImageDraw
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy, SystemOneResponse

from jev_invoice.config import Settings
from jev_invoice.decision.jev_client import JevClient
from jev_invoice.decision.questions import DOCUMENT_TYPES, PAYMENT_RISK_LEVELS
from jev_invoice.decision.service import DecisionService
from jev_invoice.domain.decision import JevAnswers
from jev_invoice.extraction.client import LLMClient
from jev_invoice.extraction.providers import LLMResponse
from jev_invoice.extraction.service import ExtractionService
from jev_invoice.extraction.vision import TranscriptionService
from jev_invoice.ingest.parsing import PageImage

JEV_MODEL = "jev-1.13.0"
ACME_BANK = "Routing 021000021 Account 4417926819"  # matches seed data

# ── LLM ───────────────────────────────────────────────────────────────────────


class FakeProviderError(Exception):
    """Retryable by default."""


class PermanentProviderError(FakeProviderError):
    pass


class FakeLLMProvider:
    """Returns scripted responses (dicts become JSON) or raises scripted errors."""

    name = "fake"
    model = "fake-model"
    permanent_errors: tuple[type[Exception], ...] = (PermanentProviderError,)
    retryable_errors: tuple[type[Exception], ...] = (FakeProviderError,)

    def __init__(self, *responses: dict[str, Any] | str | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        json_mode: bool,
        images: Sequence[PageImage] = (),
    ) -> LLMResponse:
        self.calls.append(
            {"system": system, "user": user, "json_mode": json_mode, "images": list(images)}
        )
        if not self._responses:
            raise AssertionError("FakeLLMProvider received more calls than scripted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return LLMResponse(text=text, input_tokens=1000, output_tokens=200, model=self.model)


def acme_invoice_fields(**overrides: Any) -> dict[str, Any]:
    """Extraction output for a clean Acme invoice against fully received PO-1003."""
    fields: dict[str, Any] = {
        "vendor_name": "Acme Supplies Inc",
        "invoice_number": "AS-20431",
        "invoice_date": "2026-09-30",
        "due_date": "2026-10-30",
        "currency": "USD",
        "po_reference": "PO-1003",
        "bank_account": ACME_BANK,
        "subtotal": 1074.00,
        "discount": None,
        "tax": 0,
        "total": 1074.00,
        "line_items": [
            {
                "description": "Toner cartridge, black, high yield",
                "quantity": 12,
                "unit_price": 89.50,
                "amount": 1074.00,
            }
        ],
    }
    fields.update(overrides)
    return fields


# ── Jev ───────────────────────────────────────────────────────────────────────


def wire_answers(
    *,
    document_type: str = "invoice",
    document_type_confidence: float = 0.97,
    is_payable: float = 0.96,
    prompt_injection: float = 0.02,
    bank_details_change: float = 0.03,
    urgent_pressure: float = 0.04,
    unusual_payment_method: float = 0.01,
    payment_risk: float = 0.1,
    payment_risk_confidence: float = 0.9,
) -> dict[str, Any]:
    """Answers in TypeSafe's wire format. Defaults describe a clean invoice."""
    others = (1 - document_type_confidence) / (len(DOCUMENT_TYPES) - 1)
    return {
        "document_type": {
            "type": "choice",
            "choice": document_type,
            "confidence": document_type_confidence,
            "probabilities": {
                key: document_type_confidence if key == document_type else others
                for key in DOCUMENT_TYPES
            },
        },
        "is_payable": {"type": "noul", "noul": is_payable},
        "prompt_injection": {"type": "noul", "noul": prompt_injection},
        "bank_details_change": {"type": "noul", "noul": bank_details_change},
        "urgent_pressure": {"type": "noul", "noul": urgent_pressure},
        "unusual_payment_method": {"type": "noul", "noul": unusual_payment_method},
        "payment_risk": {
            "type": "score",
            "score": payment_risk,
            "confidence": payment_risk_confidence,
            "legend": {str(i): text for i, text in enumerate(PAYMENT_RISK_LEVELS)},
            "probabilities": {"0": 0.8, "1": 0.15, "2": 0.05},
        },
    }


def response_body(answers: dict[str, Any], *, input_tokens: int = 850) -> dict[str, Any]:
    return {
        "model": JEV_MODEL,
        "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        "answers": answers,
    }


def system_one_response(answers: dict[str, Any]) -> SystemOneResponse:
    return SystemOneResponse.model_validate_json(json.dumps(response_body(answers)))


def clean_jev_answers(**overrides: Any) -> JevAnswers:
    values: dict[str, Any] = {
        "document_type": "invoice",
        "document_type_confidence": 0.97,
        "is_payable": 0.96,
        "prompt_injection": 0.02,
        "bank_details_change": 0.03,
        "urgent_pressure": 0.04,
        "unusual_payment_method": 0.01,
        "payment_risk": 0.1,
        "payment_risk_confidence": 0.9,
    }
    values.update(overrides)
    return JevAnswers.model_validate(values)


class FakeJevServer:
    """Scripted responses for /v1/systemone; records every request body.

    Account-coding requests (questions named line_1, line_2, ...) are answered from
    `coding` ({line question: (account code, confidence)}) when given, else with a
    deliberately unsure pick (30%), so the vendor's default account is used, as before.
    """

    def __init__(
        self,
        *responses: httpx2.Response | dict[str, Any],
        coding: dict[str, tuple[str, float]] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._coding = coding or {}
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        questions = body.get("questions") or {}
        if questions and all(
            name.startswith(("route_", "owner_", "action_")) for name in questions
        ):
            return httpx2.Response(200, json=response_body(self._coding_answers(questions)))
        if questions and all(name.startswith("finance_") for name in questions):
            return httpx2.Response(200, json=response_body(self._coding_answers(questions)))
        if questions and all(
            name.startswith(("tax_source_", "tax_treatment_")) for name in questions
        ):
            return httpx2.Response(200, json=response_body(self._coding_answers(questions)))
        if questions and all(name.startswith("line_") for name in questions):
            return httpx2.Response(200, json=response_body(self._coding_answers(questions)))
        if not self._responses:
            raise AssertionError("FakeJevServer received more requests than scripted")
        item = self._responses.pop(0)
        if isinstance(item, httpx2.Response):
            return item
        return httpx2.Response(200, json=response_body(item))

    def _coding_answers(self, questions: dict[str, Any]) -> dict[str, Any]:
        answers = {}
        for name, question in questions.items():
            options = list(question.get("criteria") or {})
            code, confidence = self._coding.get(name, (options[0] if options else "", 0.3))
            answers[name] = {
                "type": "choice",
                "choice": code,
                "confidence": confidence,
                "probabilities": {
                    option: confidence
                    if option == code
                    else (1 - confidence) / max(len(options) - 1, 1)
                    for option in options
                },
            }
        return answers

    @property
    def coding_requests(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self.requests
            if all(n.startswith("line_") for n in r.get("questions") or {"x": 0})
        ]


def error_response(status: int, message: str = "error") -> httpx2.Response:
    return httpx2.Response(status, json={"detail": message})


def make_jev_client(
    server: Callable[[httpx2.Request], httpx2.Response],
    settings: Settings,
    *,
    max_retries: int = 0,
) -> JevClient:
    sdk = AsyncTypeSafeClient(
        api_key="ts-test-key",
        model=JEV_MODEL,
        transport=httpx2.MockTransport(server),
        retry=RetryPolicy(max_retries=max_retries, backoff_initial=0, backoff_max=0),
    )
    return JevClient(sdk, settings)


# ── Documents ─────────────────────────────────────────────────────────────────


def make_pdf(lines: list[str]) -> bytes:
    """A minimal valid one-page PDF with a text layer (empty `lines` = scanned page)."""

    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    if lines:
        shown = " ".join(f"({escape(line)}) '" for line in lines)
        content = f"BT /F1 11 Tf 14 TL 50 760 Td {shown} ET"
    else:
        content = "q 0 0 1 rg 50 50 100 100 re f Q"  # a drawn box, no text
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        " /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return out


def make_image(
    size: tuple[int, int] = (1200, 1600), *, fmt: str = "PNG", pages: int = 1, orientation: int = 1
) -> bytes:
    """An image file with some dark marks; multi-page for TIFF; optional EXIF orientation."""
    frames = []
    for number in range(pages):
        image = Image.new("RGB", size, "white")
        ImageDraw.Draw(image).rectangle((50, 50 + 40 * number, 400, 80 + 40 * number), fill="black")
        frames.append(image)
    buffer = io.BytesIO()
    options: dict[str, Any] = {}
    if pages > 1:
        options = {"save_all": True, "append_images": frames[1:]}
    if orientation != 1:
        exif = Image.Exif()
        exif[0x0112] = orientation
        options["exif"] = exif
    frames[0].save(buffer, format=fmt, **options)
    return buffer.getvalue()


def make_email(
    *,
    body: str = "Please find our invoice attached.",
    pdf: bytes | None = None,
    image: bytes | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "Accounts <accounts@acmesupplies.co.uk>"
    message["To"] = "ap@northwind.example"
    message["Subject"] = "Invoice AS-20431"
    message.set_content(body)
    if pdf is not None:
        message.add_attachment(pdf, maintype="application", subtype="pdf", filename="AS-20431.pdf")
    if image is not None:
        message.add_attachment(image, maintype="image", subtype="jpeg", filename="photo.jpg")
    return message.as_bytes()


def groq_sdk(completion: Any, headers: dict[str, str] | None = None) -> MagicMock:
    """A stand-in Groq SDK whose raw chat completion returns *completion* and *headers*."""
    sdk = MagicMock()
    sdk.chat.completions.with_raw_response.create = AsyncMock(
        return_value=SimpleNamespace(
            parse=AsyncMock(return_value=completion), headers=headers or {}
        )
    )
    return sdk


ACME_INVOICE_TEXT = """\
INVOICE AS-20431
From: Acme Supplies Inc
Date: 2026-09-30   Due: 2026-10-30   PO: PO-1003
Toner cartridge, black, high yield   12 x 89.50   1,074.00
Total due: USD 1,074.00
Pay to: Routing 021000021 Account 4417926819
"""


# ── App wiring ────────────────────────────────────────────────────────────────


def install_fakes(
    app: FastAPI,
    settings: Settings,
    *llm_responses: dict[str, Any] | str | Exception,
    jev: list[Any] | None = None,
    jev_configured: bool = True,
    vision: list[Any] | None = None,
    coding: dict[str, tuple[str, float]] | None = None,
) -> tuple[FakeLLMProvider, FakeJevServer]:
    """Replace the running app's extraction, vision, and decision services with fakes.

    `vision` scripts the transcription model's replies (one per page); None disables it.
    """
    provider = FakeLLMProvider(*llm_responses)
    server = FakeJevServer(*(jev or []), coding=coding)
    app.state.extraction = ExtractionService(LLMClient(provider, settings))
    client = make_jev_client(server, settings) if jev_configured else None
    app.state.decisions = DecisionService(client, settings)
    app.state.transcription = (
        TranscriptionService(LLMClient(FakeLLMProvider(*vision), settings))
        if vision is not None
        else None
    )
    return provider, server
