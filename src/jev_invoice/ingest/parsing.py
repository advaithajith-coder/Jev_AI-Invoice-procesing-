"""Turn an uploaded file into text, or into page images when there is no text to read.

Supported:
- PDF with a text layer: text is extracted directly.
- Scanned PDF (no text layer): pages are rendered to images for vision transcription.
- Images (PNG, JPEG, WEBP, TIFF incl. multi-page): normalised page images for transcription.
  Phone photos are rotated upright from EXIF and scaled down.
- Plain text.
- Email (.eml): the first PDF attachment, else the first image attachment, else the body.

`sha256` identifies the invoice document itself (the attachment for emails), so the same
file forwarded in two different emails is still recognised as a duplicate.
"""

import email
import email.policy
import hashlib
import io
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr

import pdfplumber
import structlog
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

from jev_invoice.core.errors import AppError
from jev_invoice.domain.enums import InvoiceSource

log = structlog.get_logger(__name__)

_PDF_MAGIC = b"%PDF-"
_MIN_TEXT_CHARS = 20
_PDF_RENDER_DPI = 150
_MAX_PIXELS = 60_000_000  # refuse decompression bombs before decoding
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t]+")

DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_IMAGE_PX = 2000


class UnsupportedDocumentError(AppError):
    status_code = 415
    code = "unsupported_document"


class UnreadableDocumentError(AppError):
    status_code = 422
    code = "unreadable_document"


@dataclass(frozen=True)
class PageImage:
    data: bytes
    mime_type: str


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    sha256: str
    kind: str  # "pdf" | "text" | "email" | "image"
    source: InvoiceSource
    filename: str
    content_type: str
    size_bytes: int
    page_count: int | None = None
    needs_ocr: bool = False
    page_images: tuple[PageImage, ...] = field(default=(), repr=False)
    sender_email: str | None = None
    attachment_name: str | None = None


def image_mime_type(data: bytes) -> str | None:
    """Sniff a supported image type from its first bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return None


def parse_document(
    data: bytes,
    filename: str,
    content_type: str | None = None,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_image_px: int = DEFAULT_MAX_IMAGE_PX,
) -> ParsedDocument:
    """Parse *data* by sniffing its type from bytes, extension, and declared content type."""
    if not data:
        raise UnreadableDocumentError("The uploaded file is empty.")
    name = filename.lower()
    declared = (content_type or "").lower()

    if data.startswith(_PDF_MAGIC):
        return _parse_pdf(data, filename, max_pages=max_pages, max_image_px=max_image_px)
    if mime := image_mime_type(data):
        return _parse_image(data, filename, mime, max_pages=max_pages, max_image_px=max_image_px)
    if name.endswith(".eml") or declared == "message/rfc822":
        return _parse_email(data, filename, max_pages=max_pages, max_image_px=max_image_px)
    if name.endswith((".txt", ".text")) or declared.startswith("text/plain"):
        return _parse_text(data, filename)
    if name.endswith((".heic", ".heif")) or "heic" in declared:
        raise UnsupportedDocumentError(
            "HEIC photos are not supported yet. Export the photo as JPG or PNG and upload that."
        )
    raise UnsupportedDocumentError(
        "Unsupported file type. Upload a PDF, a photo or scan (PNG, JPG, WEBP, TIFF),"
        " a .txt file, or an .eml email.",
        details={"filename": filename, "content_type": content_type},
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── PDF ───────────────────────────────────────────────────────────────────────


def _parse_pdf(data: bytes, filename: str, *, max_pages: int, max_image_px: int) -> ParsedDocument:
    page_images: tuple[PageImage, ...] = ()
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
            text = "\n\n".join(page.strip() for page in pages if page.strip())
            needs_ocr = len(text) < _MIN_TEXT_CHARS
            if needs_ocr:
                page_images = tuple(
                    _normalise(page.to_image(resolution=_PDF_RENDER_DPI).original, max_image_px)
                    for page in pdf.pages[:max_pages]
                )
    except Exception as exc:  # pdfplumber raises several unrelated types for bad files
        raise UnreadableDocumentError(
            "The PDF could not be read.", details={"filename": filename}
        ) from exc
    if needs_ocr:
        log.info("pdf_without_text_layer", filename=filename, pages=len(pages))
    return ParsedDocument(
        text="" if needs_ocr else text,
        sha256=_sha256(data),
        kind="pdf",
        source=InvoiceSource.UPLOAD,
        filename=filename,
        content_type="application/pdf",
        size_bytes=len(data),
        page_count=len(pages),
        needs_ocr=needs_ocr,
        page_images=page_images,
    )


# ── Images ────────────────────────────────────────────────────────────────────


def _parse_image(
    data: bytes, filename: str, mime: str, *, max_pages: int, max_image_px: int
) -> ParsedDocument:
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > _MAX_PIXELS:
                raise UnreadableDocumentError(
                    "The image is too large to process.", details={"pixels": width * height}
                )
            frames = [frame.copy() for frame in ImageSequence.Iterator(image)]
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise UnreadableDocumentError(
            "The image could not be read.", details={"filename": filename}
        ) from exc
    return ParsedDocument(
        text="",
        sha256=_sha256(data),
        kind="image",
        source=InvoiceSource.UPLOAD,
        filename=filename,
        content_type=mime,
        size_bytes=len(data),
        page_count=len(frames),
        needs_ocr=True,
        page_images=tuple(_normalise(frame, max_image_px) for frame in frames[:max_pages]),
    )


def _normalise(image: Image.Image, max_px: int) -> PageImage:
    """Upright (EXIF), RGB, longest side at most *max_px*, JPEG-encoded."""
    upright = ImageOps.exif_transpose(image) or image
    rgb = upright.convert("RGB")
    rgb.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=85, optimize=True)
    return PageImage(data=buffer.getvalue(), mime_type="image/jpeg")


# ── Text and email ────────────────────────────────────────────────────────────


def _parse_text(data: bytes, filename: str) -> ParsedDocument:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return ParsedDocument(
        text=text.strip(),
        sha256=_sha256(data),
        kind="text",
        source=InvoiceSource.UPLOAD,
        filename=filename,
        content_type="text/plain",
        size_bytes=len(data),
    )


def _parse_email(
    data: bytes, filename: str, *, max_pages: int, max_image_px: int
) -> ParsedDocument:
    message = email.message_from_bytes(data, policy=email.policy.default)
    if not isinstance(message, EmailMessage):  # pragma: no cover - policy.default guarantees it
        raise UnreadableDocumentError("The email could not be read.")
    sender = parseaddr(str(message.get("From", "")))[1] or None

    attachments: list[tuple[str, bytes]] = []
    for part in message.iter_attachments():
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and payload:
            attachments.append((part.get_filename() or "attachment", payload))

    # Prefer a PDF attachment, then an image (a photographed or scanned invoice).
    for name, payload in attachments:
        if payload.startswith(_PDF_MAGIC):
            inner = _parse_pdf(payload, name, max_pages=max_pages, max_image_px=max_image_px)
            return _as_email(inner, data, filename, sender)
    for name, payload in attachments:
        if mime := image_mime_type(payload):
            inner = _parse_image(
                payload, name, mime, max_pages=max_pages, max_image_px=max_image_px
            )
            return _as_email(inner, data, filename, sender)

    body = message.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body is not None else ""
    if body is not None and body.get_content_type() == "text/html":
        text = _TAGS.sub(" ", text)
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in str(text).splitlines()).strip()
    if not text:
        raise UnreadableDocumentError("The email has no attachment and no readable body.")
    return ParsedDocument(
        text=text,
        sha256=_sha256(text.encode()),
        kind="email",
        source=InvoiceSource.EMAIL,
        filename=filename,
        content_type="message/rfc822",
        size_bytes=len(data),
        sender_email=sender,
    )


def _as_email(
    inner: ParsedDocument, data: bytes, filename: str, sender: str | None
) -> ParsedDocument:
    return ParsedDocument(
        text=inner.text,
        sha256=inner.sha256,
        kind="email",
        source=InvoiceSource.EMAIL,
        filename=filename,
        content_type="message/rfc822",
        size_bytes=len(data),
        page_count=inner.page_count,
        needs_ocr=inner.needs_ocr,
        page_images=inner.page_images,
        sender_email=sender,
        attachment_name=inner.filename,
    )
