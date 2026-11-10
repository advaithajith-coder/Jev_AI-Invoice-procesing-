"""Code each invoice line to an account in the chart of accounts.

The order of trust, as an AP team would apply it:

1. po       the line matched a PO line that already names an account (the buyer coded it)
2. ai       Jev chose one account from the chart, confidently (a closed Choice question
            per line, with each account's description as its option text)
3. vendor   the vendor's default account
4. suspense nothing fits: the suspense account, for someone to recode

Jev can only pick an account that exists and is active, so it cannot invent one. Its
confidence is kept with the line. People can change any line's account on the invoice
page; that is recorded as "person".
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typesafe_sdk import Choice, ChoiceAnswer, Question

from jev_invoice.accounting.controls import load_policy
from jev_invoice.coding.classification import classify_lines
from jev_invoice.coding.corrections import learned_accounts, matching, provenance, record_use
from jev_invoice.core.errors import AppError
from jev_invoice.db.models import GLAccount, InvoiceLine, PurchaseOrder, PurchaseOrderLine, Vendor
from jev_invoice.decision.jev_client import JevClient

log = structlog.get_logger(__name__)

QUESTION_SET_VERSION = "c2"
CODING_ACCOUNT_TYPES = ("expense", "cost_of_sales", "asset")


@dataclass(frozen=True)
class LineCoding:
    line_no: int
    description: str
    amount: Decimal | None
    account: str
    source: str  # po | ai | vendor | suspense
    confidence: float | None = None
    """Jev's confidence in its own choice, even when another source won."""
    suggestion: str | None = None
    """Jev's choice when it was not confident enough to use."""


@dataclass
class CodingResult:
    lines: list[LineCoding] = field(default_factory=list)
    model: str | None = None
    jev_status: str = "not_asked"  # ok | not_asked | not_configured | unavailable
    latency_ms: float | None = None
    cost_usd: Decimal = Decimal(0)
    questions: dict[str, str] = field(default_factory=dict)
    classification: dict[str, Any] = field(default_factory=dict)


class CodingService:
    def __init__(
        self,
        session: AsyncSession,
        jev: JevClient | None,
        *,
        suspense_account: str,
        min_confidence: float,
    ) -> None:
        self._session = session
        self._jev = jev
        self._suspense = suspense_account
        self._min_confidence = min_confidence

    async def accounts(self) -> list[GLAccount]:
        """Active accounts Jev may choose from (not the suspense account)."""
        return list(
            await self._session.scalars(
                select(GLAccount)
                .where(
                    GLAccount.is_active,
                    GLAccount.account_type.in_(CODING_ACCOUNT_TYPES),
                    GLAccount.code != self._suspense,
                )
                .order_by(GLAccount.code)
            )
        )

    async def code(
        self,
        lines: Sequence[InvoiceLine],
        *,
        vendor: Vendor | None,
        purchase_order: PurchaseOrder | None,
        invoice_description: str | None,
        document_text: str = "",
        buyer: str | None = None,
        currency: str | None = None,
        invoice_date: date | None = None,
        industry_context: str | None = None,
    ) -> CodingResult:
        """Choose an account for every line and write it onto the line."""
        result = CodingResult()
        reused = await matching(
            self._session,
            vendor_id=vendor.id if vendor else None,
            buyer=buyer,
            currency=currency,
            invoice_date=invoice_date,
            lines=lines,
        )
        po_accounts = _po_line_accounts(purchase_order)
        uncoded = [
            line
            for line in lines
            if po_accounts.get(line.purchase_order_line_id) is None
            and "gl_account" not in reused.get(line.line_no, {})
        ]
        learned = await learned_accounts(
            self._session,
            vendor_id=vendor.id if vendor else None,
            buyer=buyer,
            currency=currency,
            invoice_date=invoice_date,
            lines=uncoded,
        )
        learned = {
            number: evidence
            for number, evidence in learned.items()
            if evidence[0] != self._suspense
        }
        to_ask = [line for line in uncoded if line.line_no not in learned]

        choices: dict[int, ChoiceAnswer] = {}
        if to_ask:
            choices = await self._ask_jev(
                to_ask, vendor, invoice_description, result, industry_context
            )

        fallback = vendor.default_gl_account_code if vendor else None
        for line in lines:
            po_account = po_accounts.get(line.purchase_order_line_id)
            answer = choices.get(line.line_no)
            confidence = float(answer.confidence) if answer is not None else None
            if po_account:
                account, source = po_account, "po"
            elif correction := reused.get(line.line_no, {}).get("gl_account"):
                account, source = correction.value, "validated_correction"
                record_use(self._session, correction, line)
            elif line.line_no in learned:
                account, source = learned[line.line_no][0], "learned_correction"
            elif (
                answer is not None and confidence is not None and confidence >= self._min_confidence
            ):
                account, source = answer.choice, "ai"
            elif fallback:
                account, source = fallback, "vendor"
            else:
                account, source = self._suspense, "suspense"
            suggestion = answer.choice if answer is not None and source != "ai" else None
            line.gl_account_code = account
            line.gl_source = source
            line.gl_confidence = confidence
            result.lines.append(
                LineCoding(
                    line_no=line.line_no,
                    description=line.description,
                    amount=line.amount,
                    account=account,
                    source=source,
                    confidence=confidence,
                    suggestion=suggestion,
                )
            )
        result.classification = await classify_lines(
            lines,
            purchase_order,
            self._jev,
            invoice_description,
            self._min_confidence,
            document_text,
            accounting_policy=(await load_policy(self._session)).model_dump(mode="json"),
            corrections={
                number: {field: provenance(row) for field, row in fields.items()}
                for number, fields in reused.items()
            },
            industry_context=industry_context,
        )
        for line in lines:
            row = reused.get(line.line_no, {}).get("purchase_category")
            fields = (line.finance_classification or {}).get("fields", {})
            if row and fields.get("purchase_category", {}).get("source") == "validated_correction":
                record_use(self._session, row, line)
            gl = reused.get(line.line_no, {}).get("gl_account")
            if gl and line.gl_source == "validated_correction":
                fields["gl_account"] = provenance(gl)
            if line.gl_source == "learned_correction":
                bills = learned[line.line_no][1]
                fields["gl_account"]["evidence"] = (
                    f"Same account manually selected on {len(bills)} earlier approved bills."
                )
                fields["gl_account"]["source_invoice_ids"] = bills
        result.cost_usd += result.classification.get("cost_usd", Decimal(0))
        return result

    async def _ask_jev(
        self,
        lines: list[InvoiceLine],
        vendor: Vendor | None,
        invoice_description: str | None,
        result: CodingResult,
        industry_context: str | None = None,
    ) -> dict[int, ChoiceAnswer]:
        if self._jev is None:
            result.jev_status = "not_configured"
            return {}
        accounts = await self.accounts()
        if not accounts:
            return {}
        state, questions = build_coding_request(lines, accounts, vendor, invoice_description)
        if industry_context:
            state["company_industry_context"] = industry_context
        result.questions = {key: question_text(lines[i]) for i, key in enumerate(questions)}
        try:
            answer = await self._jev.ask(json.dumps(state), questions)
        except Exception as exc:
            message = exc.message if isinstance(exc, AppError) else type(exc).__name__
            log.warning("coding_jev_unavailable", error=message)
            result.jev_status = "unavailable"
            return {}
        result.jev_status = "ok"
        result.model = answer.model
        result.latency_ms = answer.latency_ms
        result.cost_usd = answer.cost_usd
        valid = {account.code for account in accounts}
        choices: dict[int, ChoiceAnswer] = {}
        for line in lines:
            reply = answer.response.answers.get(f"line_{line.line_no}")
            if isinstance(reply, ChoiceAnswer) and reply.choice in valid:
                choices[line.line_no] = reply
        return choices


def build_coding_request(
    lines: Sequence[InvoiceLine],
    accounts: Sequence[GLAccount],
    vendor: Vendor | None,
    invoice_description: str | None,
) -> tuple[dict[str, Any], dict[str, Question]]:
    """State and one Choice question per line; the options are the accounts.

    The option text is each account's name and description, which is why good account
    descriptions make good coding.
    """
    options = {account.code: f"{account.name}: {account.description}" for account in accounts}
    state: dict[str, Any] = {
        "vendor": vendor.name if vendor else None,
        "vendor_usual_account": (
            next(
                (a.name for a in accounts if vendor and a.code == vendor.default_gl_account_code),
                None,
            )
        ),
        "invoice_description": invoice_description,
        "lines": [{"description": line.description} for line in lines],
    }
    questions: dict[str, Question] = {
        f"line_{line.line_no}": Choice(instructions=line_instructions(index), criteria=options)
        for index, line in enumerate(lines)
    }
    return state, questions


def line_instructions(index: int) -> str:
    """What Jev is asked for the line at *index* (the options are the active accounts)."""
    return (
        f"Which account in the chart of accounts should the cost in "
        f"`lines[{index}].description` be recorded in? Choose by what was bought, "
        f"using the vendor and invoice description as context."
    )


def question_text(line: InvoiceLine) -> str:
    return f"Which account should '{line.description}' be recorded in?"


def _po_line_accounts(po: PurchaseOrder | None) -> dict[Any, str | None]:
    if po is None:
        return {}
    lines: list[PurchaseOrderLine] = list(po.lines)
    return {line.id: line.gl_account_code for line in lines}
