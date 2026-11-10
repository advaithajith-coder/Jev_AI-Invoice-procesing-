"""Explicit model and input limits must never masquerade as complete reading."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jev_invoice.core.errors import IncompleteDocumentError
from jev_invoice.extraction.client import LLMClient
from jev_invoice.extraction.providers import LLMResponse
from jev_invoice.extraction.service import ExtractionService
from jev_invoice.extraction.vision import TranscriptionService
from jev_invoice.ingest.parsing import PageImage, parse_document
from tests.conftest import make_settings
from tests.fakes import FakeLLMProvider, make_email, make_image


async def test_valid_but_truncated_json_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeLLMProvider()
    response = AsyncMock(
        return_value=LLMResponse(
            text='{"total": "100"}',
            input_tokens=100,
            output_tokens=200,
            model="fake",
            truncated=True,
        )
    )
    monkeypatch.setattr(provider, "complete", response)
    service = ExtractionService(LLMClient(provider, make_settings(tmp_path)))
    with pytest.raises(IncompleteDocumentError, match="output limit"):
        await service.extract("Invoice total is 100")


async def test_vision_truncation_keeps_original_page_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeLLMProvider()
    response = AsyncMock(
        return_value=LLMResponse(
            text="Only first few invoice rows",
            input_tokens=100,
            output_tokens=200,
            model="fake",
            truncated=True,
        )
    )
    monkeypatch.setattr(provider, "complete", response)
    service = TranscriptionService(LLMClient(provider, make_settings(tmp_path)))
    result = await service.transcribe([PageImage(b"fake", "image/jpeg", page_number=4)])
    assert result.truncated_pages == (4,)
    assert result.page_texts == ((4, "Only first few invoice rows"),)


def test_email_attachment_preserves_omitted_pages() -> None:
    data = make_email(image=make_image((300, 400), fmt="TIFF", pages=3))
    parsed = parse_document(data, "invoice.eml", max_pages=2)
    assert parsed.page_count == 3
    assert parsed.omitted_pages == (3,)
    assert [page.page_number for page in parsed.page_images] == [1, 2]
