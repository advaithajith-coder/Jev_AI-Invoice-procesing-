"""How every step works, for the Workflow page: models, prompts, questions and rules.

Invoice pages show only what each step produced; the how is described here, once, from
the running configuration, so it always matches what the pipeline does.
"""

from decimal import Decimal
from typing import Any

from jev_invoice.coding.service import line_instructions
from jev_invoice.config import Settings
from jev_invoice.decision.policy import ACTION_PRIORITY, DEFAULT_THRESHOLDS, POLICY_VERSION
from jev_invoice.decision.questions import QUESTION_SET_VERSION, build_questions
from jev_invoice.domain.checks import REQUIRED_FIELDS
from jev_invoice.extraction.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from jev_invoice.extraction.vision import OCR_TASK_PROMPT, TRANSCRIPTION_PROMPT
from jev_invoice.web import labels


def rules_context(settings: Settings, approval_limit: Decimal) -> dict[str, Any]:
    t = DEFAULT_THRESHOLDS
    vision = settings.vision_model or "not configured"
    ocr_model = "ocr" in vision.lower()
    base = settings.base_currency
    return {
        # One entry per flowchart node, keyed like the nodes: (title, done by, what it does).
        "info": {
            "arrive": (
                "Received",
                "Code",
                "Stores the original and its SHA-256 fingerprint; the same file twice is"
                " processed once.",
            ),
            "read": (
                "Read the file",
                "Code",
                "Takes the text layer of a PDF, the body or attachment of an email, or a text"
                " file. Scans and photos have no text and go to the vision step.",
            ),
            "vision": (
                "Vision transcription",
                f"{vision}, on this machine (Ollama)",
                f"Scans and photos only: up to {settings.vision_max_pages} pages, each scaled"
                f" to {settings.vision_max_image_px} px. Page images never leave this machine.",
            ),
            "extract": (
                "Extract fields",
                f"{settings.groq_model} on Groq",
                f"Reads the fields and lines as JSON (prompt {PROMPT_VERSION}). When Groq's"
                " per-minute allowance is low, the invoice waits before this step starts.",
            ),
            "doc": ("Document checks", "Code", "Checks the invoice on its own."),
            "md": (
                "Vendor and PO checks",
                "Code",
                "Checks the invoice against the vendor list, purchase orders and deliveries.",
            ),
            "jev": (
                "Jev judgment",
                f"TypeSafe Jev, {settings.typesafe_model}",
                f"Reads the document text and answers these questions (set"
                f" {QUESTION_SET_VERSION}) with probabilities.",
            ),
            "coding": (
                "Account coding",
                "PO, then Jev",
                f"Each line gets the PO line's account; else Jev's choice when it is at least"
                f" {settings.coding_min_confidence:.0%} sure; else the vendor's default account;"
                f" else suspense ({settings.export_suspense_account}).",
            ),
            "policy": (
                "Decision",
                f"Fixed policy {POLICY_VERSION}",
                "Turns every finding and Jev answer into reasons and picks the outcome."
                " Approved automatically only with no reasons at all.",
            ),
            "review": (
                "Review queue",
                "A person",
                "Every invoice that is not approved automatically waits here with its reasons."
                " A person approves or rejects it; a blocked invoice needs a note on how it"
                " was verified.",
            ),
        },
        "document_checks": [
            (
                "Required fields",
                f"{', '.join(f.replace('_', ' ') for f in REQUIRED_FIELDS)} must all be read.",
            ),
            (
                "Amounts add up",
                f"Lines, subtotal, discount, shipping, tax and total agree within"
                f" {settings.amount_tolerance}; total minus already paid equals the balance due.",
            ),
            (
                "Dates make sense",
                "Not more than 1 day in the future, due on or after the invoice"
                f" date, and not older than {settings.stale_invoice_days} days.",
            ),
            (
                "Currency",
                f"In {base}, or converted to {base} at the European Central Bank rate for"
                " the invoice date. With no rate, a person decides.",
            ),
            ("Billed to the company", f"Addressed to {settings.company_name}."),
            (
                "Approval limit",
                f"Total (converted to {base}) at most {approval_limit:,.2f}"
                f" {base}; above it, a budget owner approves. Change it on the Settings page.",
            ),
        ],
        "vendor_checks": [
            (
                "Vendor",
                "In the vendor list (name compared without capitals, punctuation or endings"
                " like Inc and LLC), and not on hold or blocked.",
            ),
            (
                "Bank account",
                "The account on the invoice is the one on file for the vendor."
                " Compared by fingerprint; full numbers are never stored.",
            ),
            (
                "Sender domain",
                "An emailed invoice comes from the vendor's domain or a subdomain;"
                " a domain at least 75% alike but different is called out as a lookalike.",
            ),
            (
                "Tax ID",
                "Matches the vendor's, ignoring formatting. Flagged only when both are"
                " known and differ.",
            ),
            ("Currency", "The invoice's currency is the vendor's usual one and the PO's."),
            ("Duplicate", "The same vendor and invoice number was not received before."),
            (
                "Possible duplicate",
                f"No invoice from the vendor for the same amount within"
                f" {settings.duplicate_window_days} days, or with a lookalike number (O/0, l/1, a"
                " short suffix).",
            ),
            (
                "Purchase order",
                "Quoted when the vendor requires one, found, the vendor's own, and"
                " not cancelled or closed; the invoice is dated after it.",
            ),
            (
                "Lines on the PO",
                "Each line pairs with a PO line by its description (about a third"
                " of the words in common) or by a unique price.",
            ),
            (
                "Prices",
                f"Each unit price is within {settings.po_price_tolerance_pct.normalize():f}%"
                f" or {settings.po_tolerance_max_amount:,.2f} {base} of the PO, whichever is"
                " smaller.",
            ),
            ("Quantities", "Units billed so far are no more than were received and ordered."),
        ],
        "thresholds": [
            ("No risk", f"A risk answer of {t.risk_clear:.0%} or less."),
            (
                "Risk present",
                f"{t.fraud_signal:.0%} or more ({t.injection_signal:.0%} for"
                " instructions aimed at software: a false alarm only costs one review).",
            ),
            ("In between", 'Counts as "Jev unsure": a person decides.'),
            (
                "Document type",
                f"Acted on only at {t.auto_choice_confidence:.0%} confidence or more.",
            ),
            (
                "Asks for payment",
                f"{t.not_payable:.0%} or less means it does not; approving"
                f" automatically needs {t.auto_is_payable:.0%} or more.",
            ),
            (
                "Payment risk",
                f"0 (low) to 2 (high): {t.moderate_risk} or more is moderate,"
                f" {t.high_risk} or more is high.",
            ),
        ],
        "policy": [
            (
                priority,
                labels.ACTIONS[action][0],
                labels.ACTIONS[action][1],
                [labels.reason(code) for code in sorted(codes)],
            )
            for priority, (action, _band, codes) in enumerate(ACTION_PRIORITY, start=1)
        ],
        "vision_prompt": OCR_TASK_PROMPT if ocr_model else TRANSCRIPTION_PROMPT,
        "extraction_prompt": SYSTEM_PROMPT,
        "extraction_input": build_user_prompt("…the text read from the file…"),
        "jev_questions": [
            (name, str(getattr(question, "instructions", "")), _options(question))
            for name, question in build_questions().items()
        ],
        "coding_question": line_instructions(0),
    }


def _options(question: Any) -> list[str]:
    """A Choice's or Score's options as text; a yes/no question has none."""
    criteria = getattr(question, "criteria", None)
    if isinstance(criteria, dict):
        return [str(text) for text in criteria.values()]
    if isinstance(criteria, list | tuple):
        return [str(text) for text in criteria]
    return []
