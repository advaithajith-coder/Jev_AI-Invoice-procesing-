"""The review screens: upload, queue, invoice page, and the human decision."""

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jev_invoice.config import Settings
from tests.fakes import (
    ACME_INVOICE_TEXT,
    acme_invoice_fields,
    install_fakes,
    make_image,
    make_pdf,
    wire_answers,
)

FRAUD_ACCOUNT = "GB11 FAKE 0400 7519 2837 46"


def _upload(
    client: TestClient,
    content: bytes = ACME_INVOICE_TEXT.encode(),
    name: str = "inv.txt",
    content_type: str = "text/plain",
) -> str:
    """Upload through the HTML form; return the invoice page path it redirects to."""
    response = client.post(
        "/upload", files={"file": (name, content, content_type)}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    location: str = response.headers["location"]
    assert location.startswith("/invoices/")
    return location.split("?")[0]


def _task_id(page: str) -> str:
    match = re.search(r'name="task_id" value="([0-9a-f-]+)"', page)
    assert match, "the page should show the review form"
    return match.group(1)


def test_inbox_shows_upload_form_and_empty_state(client: TestClient) -> None:
    page = client.get("/inbox").text

    assert 'action="/upload"' in page
    assert "No invoices yet" in page


def test_screens_are_named_after_the_company(client: TestClient) -> None:
    page = client.get("/review").text

    assert "<title>Review queue | Northwind Analytics AP</title>" in page
    assert '<span class="wordmark__name">Parser.AI</span>' in page
    assert "Northwind Analytics" in page
    assert "Invoices must be billed to Northwind Analytics Inc." in client.get("/inbox").text


def test_upload_redirects_to_invoice_page_with_plain_language_reasons(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app, seeded_settings, acme_invoice_fields(po_reference=None), jev=[wire_answers()]
    )

    path = _upload(client)
    page = client.get(path + "?notice=uploaded").text

    assert "Acme Supplies Inc" in page
    assert "Invoice processed." in page
    assert "Non-PO spend needs approval" in page
    assert "GL coding needs Finance confirmation" in page
    assert "Budget owner" in page
    assert "No purchase order" in page  # reason title, not the raw code
    assert "NO_PURCHASE_ORDER" not in page
    assert "Jev judgment" in page
    assert "USD 1,074.00" in page
    assert "account ending" in page and "6819" in page


def test_review_queue_lists_invoice_with_reasons(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app, seeded_settings, acme_invoice_fields(po_reference=None), jev=[wire_answers()]
    )
    _upload(client)

    page = client.get("/review").text

    assert "Waiting for review" in page
    assert "Acme Supplies Inc" in page
    assert "No purchase order" in page


def test_reviewer_approves_from_the_page(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app, seeded_settings, acme_invoice_fields(po_reference=None), jev=[wire_answers()]
    )
    path = _upload(client)
    task_id = _task_id(client.get(path).text)

    response = client.post(
        f"{path}/review",
        data={"task_id": task_id, "verdict": "approve", "reviewer": "Maya", "note": "Budget OK"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.cookies.get("reviewer") == "Maya"
    page = client.get(response.headers["location"]).text
    assert "Approved and recorded." in page
    assert "Approved by reviewer" in page  # history
    assert "Budget OK" in page
    assert 'name="task_id"' not in page  # form gone once decided
    assert "Nothing to review" in client.get("/review").text


def test_blocked_invoice_needs_a_note_to_approve(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app, seeded_settings, acme_invoice_fields(bank_account=FRAUD_ACCOUNT), jev=[wire_answers()]
    )
    path = _upload(client)
    page = client.get(path).text
    assert "Blocked: possible fraud" in page
    assert "Bank account differs from our records" in page
    task_id = _task_id(page)

    refused = client.post(
        f"{path}/review", data={"task_id": task_id, "verdict": "approve", "reviewer": "Maya"}
    )
    assert refused.status_code == 422
    assert "needs a note" in refused.text
    assert 'name="task_id"' in refused.text  # still waiting

    accepted = client.post(
        f"{path}/review",
        data={
            "task_id": task_id,
            "verdict": "approve",
            "reviewer": "Maya",
            "note": "Called Acme on 0113 496 0000; the new account is genuine.",
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303


def test_blocked_invoice_can_be_rejected_without_a_note(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app, seeded_settings, acme_invoice_fields(bank_account=FRAUD_ACCOUNT), jev=[wire_answers()]
    )
    path = _upload(client)

    response = client.post(
        f"{path}/review",
        data={"task_id": _task_id(client.get(path).text), "verdict": "reject", "reviewer": "Maya"},
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_reviewer_name_is_required(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app, seeded_settings, acme_invoice_fields(po_reference=None), jev=[wire_answers()]
    )
    path = _upload(client)

    response = client.post(
        f"{path}/review",
        data={"task_id": _task_id(client.get(path).text), "verdict": "approve", "reviewer": "  "},
    )

    assert response.status_code == 400
    assert "Enter your name" in response.text


def test_proposed_rejection_puts_reject_first(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app, seeded_settings, acme_invoice_fields(), jev=[wire_answers(document_type="quote")]
    )
    page = client.get(_upload(client)).text

    assert "Proposed: reject" in page
    assert page.index('value="reject"') < page.index('value="approve"')


def test_invoice_text_is_escaped(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app,
        seeded_settings,
        acme_invoice_fields(vendor_name="<script>alert(1)</script>"),
        jev=[wire_answers()],
    )
    hostile = (ACME_INVOICE_TEXT + "\n<img src=x onerror=alert(2)>").encode()

    page = client.get(_upload(client, hostile)).text

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "<img src=x" not in page


def test_pdf_original_served_inline(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])
    pdf = make_pdf(ACME_INVOICE_TEXT.splitlines())
    path = _upload(client, pdf, "AS-20431.pdf", "application/pdf")

    page = client.get(path).text
    document = client.get(f"{path}/document")

    assert "<iframe" in page
    assert document.content == pdf
    assert document.headers["content-type"] == "application/pdf"
    assert document.headers["x-content-type-options"] == "nosniff"


def test_unsupported_upload_shows_error_on_inbox(client: TestClient) -> None:
    response = client.post("/upload", files={"file": ("photo.png", b"\x89PNG", "image/png")})

    assert response.status_code == 415
    assert "Unsupported file type" in response.text
    assert 'role="alert"' in response.text


def test_failed_extraction_explains_next_step(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, "not json")

    page = client.get(_upload(client)).text

    assert "could not be used" in page  # the extraction step says why
    assert "Could not be read. Enter this invoice by hand" in page  # and the outcome what next


def test_duplicate_upload_goes_to_existing_invoice(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    provider, _ = install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])
    first = _upload(client)

    response = client.post(
        "/upload",
        files={"file": ("again.txt", ACME_INVOICE_TEXT.encode(), "text/plain")},
        follow_redirects=False,
    )

    assert response.headers["location"] == f"{first}?notice=duplicate"
    assert len(provider.calls) == 1


def test_inbox_filters_by_status(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(app, seeded_settings, acme_invoice_fields(), jev=[wire_answers()])
    _upload(client)

    assert "Acme Supplies Inc" in client.get("/inbox?status=approved").text
    assert "No invoices with this status" in client.get("/inbox?status=rejected").text


def test_photo_page_shows_image_and_transcription(
    app: FastAPI, client: TestClient, seeded_settings: Settings
) -> None:
    install_fakes(
        app,
        seeded_settings,
        acme_invoice_fields(),
        jev=[wire_answers()],
        vision=[ACME_INVOICE_TEXT],
    )
    photo = make_image(fmt="JPEG")
    path = _upload(client, photo, "photo.jpg", "image/jpeg")

    page = client.get(path).text
    document = client.get(f"{path}/document")

    assert f'<img class="document__image" src="{path}/document"' in page
    assert "1 page(s) read" in page  # step 3, opening to the text it read
    assert "INVOICE AS-20431" in page
    assert "Image transcribed" in page  # history
    assert document.headers["content-type"] == "image/jpeg"
    assert document.content == photo
