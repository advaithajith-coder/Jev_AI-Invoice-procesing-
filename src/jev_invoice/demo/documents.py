"""Turn a sample's text into the file a vendor would send: PDF, scanned image, or email."""

import io
from email.message import EmailMessage
from email.utils import formatdate

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Monospaced fonts keep the invoice columns lined up; the first one found is used.
_MONO_FONTS = ("DejaVuSansMono.ttf", "consola.ttf", "cour.ttf", "Menlo.ttc", "Courier New.ttf")
_AP_ADDRESS = "ap@northwindanalytics.com"


def pdf_from_text(text: str) -> bytes:
    """A one-page PDF with a real text layer, set in Courier like a printed invoice."""

    def escape(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    shown = " ".join(f"({escape(line)}) '" for line in text.splitlines())
    content = f"BT /F1 10 Tf 13 TL 40 770 Td {shown} ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        " /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
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


def scan_from_text(text: str) -> bytes:
    """A PNG that looks like a scanned page: off-white, slightly skewed and soft."""
    font = _mono_font(24)
    page = Image.new("RGB", (1240, 1754), (248, 246, 240))
    draw = ImageDraw.Draw(page)
    y = 110
    for line in text.splitlines():
        draw.text((90, y), line, fill=(30, 30, 34), font=font)
        y += 36
    page = page.rotate(0.7, resample=Image.Resampling.BICUBIC, fillcolor=(248, 246, 240))
    page = page.filter(ImageFilter.GaussianBlur(0.6))
    buffer = io.BytesIO()
    page.save(buffer, format="PNG")
    return buffer.getvalue()


def email_with_attachment(
    *, sender: str, subject: str, body: str, attachment: bytes, filename: str
) -> bytes:
    message = _email(sender=sender, subject=subject, body=body)
    message.add_attachment(attachment, maintype="application", subtype="pdf", filename=filename)
    return message.as_bytes()


def email_with_body(*, sender: str, subject: str, body: str) -> bytes:
    return _email(sender=sender, subject=subject, body=body).as_bytes()


def _email(*, sender: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = _AP_ADDRESS
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message.set_content(body)
    return message


def _mono_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in _MONO_FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)
