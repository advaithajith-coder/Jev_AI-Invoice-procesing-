import hashlib
from pathlib import Path

import pytest

from jev_invoice.domain.enums import InvoiceSource
from jev_invoice.ingest.parsing import (
    UnreadableDocumentError,
    UnsupportedDocumentError,
    parse_document,
)
from jev_invoice.ingest.storage import LocalFileStorage
from tests.fakes import make_email, make_pdf

PDF_LINES = ["INVOICE AS-20431", "Acme Supplies Inc", "Total due: GBP 1,074.00"]


def test_pdf_text_is_extracted() -> None:
    data = make_pdf(PDF_LINES)

    parsed = parse_document(data, "invoice.pdf", "application/pdf")

    assert parsed.kind == "pdf"
    assert "INVOICE AS-20431" in parsed.text and "1,074.00" in parsed.text
    assert parsed.page_count == 1
    assert not parsed.needs_ocr
    assert parsed.sha256 == hashlib.sha256(data).hexdigest()


def test_pdf_detected_by_content_not_extension() -> None:
    assert parse_document(make_pdf(PDF_LINES), "scan.bin").kind == "pdf"


def test_scanned_pdf_is_flagged_for_ocr() -> None:
    parsed = parse_document(make_pdf([]), "scan.pdf")

    assert parsed.needs_ocr
    assert parsed.text == ""


def test_corrupt_pdf_is_unreadable() -> None:
    with pytest.raises(UnreadableDocumentError):
        parse_document(b"%PDF-1.4 this is not really a pdf", "broken.pdf")


def test_text_file() -> None:
    parsed = parse_document("﻿INVOICE 1\nTotal 10.00\n".encode(), "inv.txt", "text/plain")

    assert parsed.kind == "text"
    assert parsed.text == "INVOICE 1\nTotal 10.00"
    assert parsed.source is InvoiceSource.UPLOAD


def test_email_uses_pdf_attachment_and_hashes_the_attachment() -> None:
    pdf = make_pdf(PDF_LINES)

    parsed = parse_document(make_email(pdf=pdf), "mail.eml", "message/rfc822")

    assert parsed.kind == "email"
    assert parsed.source is InvoiceSource.EMAIL
    assert parsed.sender_email == "accounts@acmesupplies.co.uk"
    assert parsed.attachment_name == "AS-20431.pdf"
    assert "INVOICE AS-20431" in parsed.text
    # Same PDF forwarded in a different email is still the same invoice.
    assert parsed.sha256 == hashlib.sha256(pdf).hexdigest()
    other = parse_document(make_email(body="FYI", pdf=pdf), "fwd.eml")
    assert other.sha256 == parsed.sha256


def test_email_without_attachment_uses_body() -> None:
    parsed = parse_document(make_email(body="INVOICE 77\nTotal: 50.00"), "mail.eml")

    assert parsed.text.startswith("INVOICE 77")
    assert parsed.attachment_name is None


@pytest.mark.parametrize(
    ("data", "name", "content_type"),
    [(b"PK\x03\x04", "invoice.docx", None), (b"\x89PNG", "scan.png", "image/png")],
)
def test_unsupported_types_rejected(data: bytes, name: str, content_type: str | None) -> None:
    with pytest.raises(UnsupportedDocumentError):
        parse_document(data, name, content_type)


def test_empty_upload_rejected() -> None:
    with pytest.raises(UnreadableDocumentError):
        parse_document(b"", "empty.pdf")


def test_storage_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    sha = hashlib.sha256(b"data").hexdigest()

    key = storage.save(b"data", sha256=sha, extension=".pdf")

    assert key == f"{sha[:2]}/{sha}.pdf"
    assert storage.save(b"data", sha256=sha, extension=".pdf") == key
    assert storage.read(key) == b"data"
    with pytest.raises(ValueError, match="escapes"):
        storage.read("../outside.txt")
