"""Extract typed invoice fields from document text."""

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from pydantic import ValidationError

from jev_invoice.core.errors import ExtractionError, IncompleteDocumentError
from jev_invoice.domain.invoice import ExtractedInvoice
from jev_invoice.extraction.client import LLMCall, LLMClient
from jev_invoice.extraction.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt

log = structlog.get_logger(__name__)

_MAX_DOCUMENT_CHARS = 60_000
_PO_LABEL = re.compile(r"\b(?:purchase\s+order(?:\s+(?:number|no\.?))?|p\.?\s*o\.?)\b", re.I)


def supported_po_reference(text: str, reference: str | None, evidence: Any) -> bool:
    """Require the reference and PO label in one contiguous printed quotation."""
    if reference is None:
        return True
    if not isinstance(evidence, str) or not _PO_LABEL.search(evidence):
        return False

    def normalize(value: str) -> str:
        return " ".join(value.split()).casefold()

    return normalize(evidence) in normalize(text) and normalize(reference) in normalize(evidence)


@dataclass(frozen=True)
class ExtractionResult:
    invoice: ExtractedInvoice
    call: LLMCall
    prompt_version: str
    raw: dict[str, Any]
    """The model's JSON with the raw bank account removed (safe to store)."""


class ExtractionService:
    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def wait_for_capacity(self) -> float:
        return await self._client.wait_for_capacity()

    async def extract(self, document_text: str) -> ExtractionResult:
        """Read invoice fields from *document_text*.

        Raises:
            ExtractionError: no text to read, the model failed, or its output was unusable.
        """
        text = document_text.strip()
        if not text:
            raise ExtractionError("The document has no readable text to extract fields from.")
        if len(text) > _MAX_DOCUMENT_CHARS:
            raise IncompleteDocumentError(
                f"The document contains {len(text):,} characters, above the extraction limit "
                f"of {_MAX_DOCUMENT_CHARS:,}. Supply a complete shorter document or increase "
                "the supported limit before processing; no text was silently discarded.",
                details={"document_characters": len(text), "character_limit": _MAX_DOCUMENT_CHARS},
            )
        call = await self._client.complete(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(text),
            json_mode=True,
        )
        if call.truncated:
            raise IncompleteDocumentError(
                "The extraction model reached its output limit. Its partial result cannot "
                "be used to approve this document.",
                details={"extraction_truncated": True},
            )
        try:
            # parse_float=Decimal keeps money exact: 0.1 + 0.2 stays 0.3.
            data = json.loads(call.text, parse_float=Decimal)
            if not isinstance(data, dict):
                raise TypeError("top-level JSON is not an object")
            invoice = ExtractedInvoice.model_validate(data)
        except (ValueError, TypeError, ValidationError) as exc:
            log.warning("extraction_output_unusable", error=type(exc).__name__)
            raise ExtractionError(
                "The extraction model returned output that could not be used.",
                details={"error_type": type(exc).__name__},
            ) from exc

        if "po_reference_evidence" in data and not supported_po_reference(
            text,
            invoice.po_reference,
            data.get("po_reference_evidence"),
        ):
            # Do not turn an unlabelled customer reference into a PO match.
            invoice.po_reference = None
            data["po_reference"] = None
            data["po_reference_evidence"] = None

        raw = {key: value for key, value in data.items() if key != "bank_account"}
        # Free-text fields can repeat the account number ("Remit to: ... Account 4417926819"):
        # keep the masked versions the model validated, never the raw text.
        for key in ("payment_terms_text", "late_fee_terms", "remittance_details", "notes"):
            if key in raw:
                raw[key] = getattr(invoice, key)
        if "other_details" in raw:
            raw["other_details"] = [detail.model_dump() for detail in invoice.other_details]
        return ExtractionResult(
            invoice=invoice,
            call=call,
            prompt_version=PROMPT_VERSION,
            raw=json.loads(json.dumps(raw, default=str)),
        )
