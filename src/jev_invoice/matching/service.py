"""Match an extracted invoice against master data.

Every check here has one right answer, so it is code, not a model:
- vendor: known? on hold or blocked? invoice currency matches the vendor's?
- bank account: does the payee account match the one on file? (keyed fingerprints)
- duplicates: the same vendor + invoice number already received (a duplicate), or the
  same vendor and amount within a few days, or an almost identical number (possible)
- purchase order: only when the vendor bills against POs. The PO must exist, belong to
  the vendor, be open, and be older than the invoice. Then a line-by-line 3-way match:
  each invoice line is paired with a PO line, its price checked against the PO price
  (2-way) and its quantity against what was delivered (3-way). When lines cannot be
  paired, the totals are compared instead.

Tolerances are "a percentage or a fixed amount, whichever is smaller", as AP teams set
them: a percentage alone lets large orders drift by large sums.
"""

import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jev_invoice.db.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    Invoice,
    InvoiceLine,
    PurchaseOrder,
    PurchaseOrderLine,
    Vendor,
)
from jev_invoice.domain.enums import InvoiceStatus, PurchaseOrderStatus, ReasonCode, VendorStatus
from jev_invoice.domain.invoice import CheckFinding, ExtractedInvoice, ExtractedLine
from jev_invoice.domain.vendors import (
    bank_account_fingerprint,
    bank_account_last4,
    normalize_vendor_name,
)

_NOT_ALNUM = re.compile(r"[^A-Z0-9]")
_WORD = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {"the", "and", "for", "with", "of", "to", "in", "on", "per", "a", "an"}
_INACTIVE_STATUSES = (InvoiceStatus.REJECTED, InvoiceStatus.FAILED)
_CENT = Decimal("0.01")
# How alike two line descriptions must be (shared words / all words) to pair them.
_LINE_SIMILARITY = 0.34
# How alike a sender domain must be to the vendor's to be called a lookalike.
_LOOKALIKE_RATIO = 0.75


def normalize_reference(value: str) -> str:
    """'inv-2026/001 ' -> 'INV2026001'. Used for invoice and PO numbers."""
    return _NOT_ALNUM.sub("", value.upper())


@dataclass(frozen=True)
class MatchCheck:
    """One check as shown to people: passed (True), failed (False), or not applicable (None)."""

    name: str
    label: str
    passed: bool | None
    detail: str


@dataclass
class MatchResult:
    vendor: Vendor | None = None
    purchase_order: PurchaseOrder | None = None
    bank_account_last4: str | None = None
    bank_account_fingerprint: str | None = None
    findings: list[CheckFinding] = field(default_factory=list)
    checks: list[MatchCheck] = field(default_factory=list)
    line_matches: dict[int, uuid.UUID] = field(default_factory=dict)
    """Invoice line index (0-based) -> the PO line it was paired with."""

    def add(self, code: ReasonCode, message: str) -> None:
        self.findings.append(CheckFinding(code=code, message=message))

    def check(self, name: str, label: str, passed: bool | None, detail: str) -> None:
        self.checks.append(MatchCheck(name, label, passed, detail))


class MatchingService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        fingerprint_key: bytes,
        price_tolerance_pct: Decimal,
        tolerance_max_amount: Decimal = Decimal("50.00"),
        duplicate_window_days: int = 7,
    ) -> None:
        self._session = session
        self._key = fingerprint_key
        self._pct = price_tolerance_pct
        self._cap = tolerance_max_amount
        self._window = timedelta(days=duplicate_window_days)

    def allowed(self, base: Decimal) -> Decimal:
        """How much above *base* is tolerated: the percentage or the cap, whichever is smaller."""
        return min(abs(base) * self._pct / Decimal(100), self._cap).quantize(_CENT)

    @property
    def tolerance_text(self) -> str:
        return f"{self._pct.normalize():f}% or {self._cap:,.2f}, whichever is smaller"

    async def match(
        self,
        invoice: ExtractedInvoice,
        *,
        invoice_id: uuid.UUID,
        sender_email: str | None = None,
        stored_bank: tuple[str | None, str | None] | None = None,
    ) -> MatchResult:
        """*sender_email*: who emailed the invoice, when it arrived by email.

        *stored_bank*: (last 4, fingerprint) kept from an earlier check, for re-checking an
        invoice whose raw account number was never stored.
        """
        result = MatchResult()
        if invoice.bank_account:
            result.bank_account_last4 = bank_account_last4(invoice.bank_account)
            result.bank_account_fingerprint = bank_account_fingerprint(
                invoice.bank_account, self._key
            )
        elif stored_bank is not None:
            result.bank_account_last4, result.bank_account_fingerprint = stored_bank

        await self._match_vendor(invoice, result)
        if result.vendor is not None:
            _check_sender(result.vendor, sender_email, result)
            _check_tax_id(result.vendor, invoice.vendor_tax_id, result)
        await self._check_duplicate(invoice, invoice_id, result)
        await self._match_purchase_order(invoice, invoice_id, result)
        return result

    # ── vendor ────────────────────────────────────────────────────────────────

    async def _match_vendor(self, invoice: ExtractedInvoice, result: MatchResult) -> None:
        if not invoice.vendor_name:
            result.check("vendor", "Vendor is in the vendor list", None, "No vendor name was read")
            return  # already reported as a missing required field
        vendor = await self._session.scalar(
            select(Vendor).where(
                Vendor.normalized_name == normalize_vendor_name(invoice.vendor_name)
            )
        )
        label = "Vendor is in the vendor list"
        if vendor is None:
            result.add(
                ReasonCode.UNKNOWN_VENDOR,
                f"'{invoice.vendor_name}' is not in the vendor list",
            )
            result.check("vendor", label, False, f"No vendor named '{invoice.vendor_name}'")
            return
        result.vendor = vendor
        result.check("vendor", label, True, f"Matched {vendor.name}")

        if vendor.status == VendorStatus.BLOCKED:
            result.add(ReasonCode.VENDOR_BLOCKED, f"{vendor.name} is blocked")
        elif vendor.status == VendorStatus.ON_HOLD:
            result.add(ReasonCode.VENDOR_ON_HOLD, f"{vendor.name} is on hold")
        result.check(
            "vendor_status",
            "Vendor is active",
            vendor.status == VendorStatus.ACTIVE,
            f"Status: {vendor.status}",
        )

        if invoice.currency:
            same = invoice.currency == vendor.default_currency
            if not same:
                result.add(
                    ReasonCode.CURRENCY_MISMATCH,
                    f"Invoice is in {invoice.currency};"
                    f" {vendor.name} bills in {vendor.default_currency}",
                )
            result.check(
                "currency",
                "Currency is the vendor's usual one",
                same,
                f"Invoice {invoice.currency}, vendor {vendor.default_currency}",
            )

        bank_label = "Bank account matches our records"
        if not result.bank_account_fingerprint:
            result.check("bank", bank_label, None, "The invoice shows no bank account")
        elif not vendor.bank_account_fingerprint:
            result.check(
                "bank",
                bank_label,
                None,
                f"No account on file to compare (invoice: ending {result.bank_account_last4})",
            )
        elif result.bank_account_fingerprint != vendor.bank_account_fingerprint:
            result.add(
                ReasonCode.BANK_ACCOUNT_MISMATCH,
                f"Invoice asks for payment to an account ending {result.bank_account_last4};"
                f" the account on file for {vendor.name} ends {vendor.bank_account_last4}",
            )
            result.check(
                "bank",
                bank_label,
                False,
                f"Invoice ends {result.bank_account_last4}, file ends {vendor.bank_account_last4}",
            )
        else:
            result.check("bank", bank_label, True, f"Both end {result.bank_account_last4}")

    # ── duplicates ────────────────────────────────────────────────────────────

    async def _check_duplicate(
        self, invoice: ExtractedInvoice, invoice_id: uuid.UUID, result: MatchResult
    ) -> None:
        label = "Not a duplicate"
        near_label = "No similar invoice from this vendor"
        same_vendor = None
        if result.vendor is not None:
            same_vendor = Invoice.vendor_id == result.vendor.id
        elif invoice.vendor_name:
            same_vendor = func.lower(Invoice.vendor_name) == invoice.vendor_name.lower()
        if same_vendor is None:
            result.check("duplicate", label, None, "No vendor to compare against")
            result.check("near_duplicate", near_label, None, "No vendor to compare against")
            return
        others = (
            Invoice.id != invoice_id,
            Invoice.status.not_in(_INACTIVE_STATUSES),
            same_vendor,
        )

        if not invoice.invoice_number:
            result.check("duplicate", label, None, "No invoice number to compare")
        else:
            number = normalize_reference(invoice.invoice_number)
            existing = (
                await self._session.execute(
                    select(Invoice.id)
                    .where(*others, Invoice.invoice_number_normalized == number)
                    .limit(1)
                )
            ).first()
            if existing is not None:
                result.add(
                    ReasonCode.DUPLICATE_INVOICE,
                    f"Invoice {invoice.invoice_number} from this vendor was already received"
                    f" (invoice id {existing.id})",
                )
                result.check(
                    "duplicate", label, False, f"{invoice.invoice_number} was received before"
                )
                return  # an exact duplicate: no need to look for near ones
            result.check("duplicate", label, True, f"{invoice.invoice_number} not seen before")

        if invoice.total is None:
            result.check("near_duplicate", near_label, None, "No total to compare")
            return
        rows = await self._session.execute(
            select(Invoice.invoice_number, Invoice.invoice_number_normalized, Invoice.invoice_date)
            .where(*others, Invoice.total == invoice.total)
            .limit(50)
        )
        number = normalize_reference(invoice.invoice_number) if invoice.invoice_number else ""
        for other_number, other_normalized, other_date in rows:
            close_dates = (
                invoice.invoice_date is not None
                and other_date is not None
                and abs(invoice.invoice_date - other_date) <= self._window
            )
            close_numbers = _similar_numbers(number, other_normalized or "")
            if close_dates or close_numbers:
                why = "an almost identical number" if close_numbers else "a date close to it"
                result.add(
                    ReasonCode.POSSIBLE_DUPLICATE,
                    f"Same amount ({invoice.total:,.2f}) as invoice {other_number or '(no number)'}"
                    f" dated {other_date or 'unknown'}, with {why}",
                )
                result.check(
                    "near_duplicate",
                    near_label,
                    False,
                    f"{other_number or 'Another invoice'} has the same amount and {why}",
                )
                return
        result.check(
            "near_duplicate",
            near_label,
            True,
            f"No other invoice for {invoice.total:,.2f} within {self._window.days} days",
        )

    # ── purchase orders ───────────────────────────────────────────────────────

    async def _match_purchase_order(
        self, invoice: ExtractedInvoice, invoice_id: uuid.UUID, result: MatchResult
    ) -> None:
        po_label = "Purchase order found and open"
        if not invoice.po_reference:
            vendor = result.vendor
            if vendor is not None and not vendor.po_required:
                result.check("po", po_label, None, f"Not needed: {vendor.name} bills without a PO")
                return
            result.add(ReasonCode.NO_PURCHASE_ORDER, "The invoice quotes no purchase order")
            result.check("po", po_label, False, "No PO number on the invoice")
            return
        reference = normalize_reference(invoice.po_reference)
        po = await self._session.scalar(
            select(PurchaseOrder)
            .where(func.replace(func.upper(PurchaseOrder.po_number), "-", "") == reference)
            .options(
                selectinload(PurchaseOrder.lines),
                selectinload(PurchaseOrder.receipts)
                .selectinload(GoodsReceipt.lines)
                .selectinload(GoodsReceiptLine.purchase_order_line),
            )
        )
        if po is None:
            result.add(ReasonCode.PO_NOT_FOUND, f"Purchase order {invoice.po_reference} not found")
            result.check("po", po_label, False, f"{invoice.po_reference} does not exist")
            return
        result.purchase_order = po

        problems = []
        if result.vendor is not None and po.vendor_id != result.vendor.id:
            result.add(
                ReasonCode.PO_VENDOR_MISMATCH,
                f"{po.po_number} was issued to a different vendor",
            )
            problems.append("issued to another vendor")
        if po.status in (PurchaseOrderStatus.CANCELLED, PurchaseOrderStatus.CLOSED):
            result.add(ReasonCode.PO_CLOSED, f"{po.po_number} is {po.status}")
            problems.append(str(po.status))
        result.check(
            "po",
            po_label,
            not problems,
            f"{po.po_number} ({po.status})" + (": " + ", ".join(problems) if problems else ""),
        )
        if invoice.currency and invoice.currency != po.currency:
            result.add(
                ReasonCode.CURRENCY_MISMATCH,
                f"Invoice is in {invoice.currency}; {po.po_number} is in {po.currency}",
            )

        if invoice.invoice_date is not None:
            after = invoice.invoice_date >= po.issued_date
            if not after:
                result.add(
                    ReasonCode.INVOICE_BEFORE_PO,
                    f"Invoice is dated {invoice.invoice_date}, before {po.po_number} was issued"
                    f" on {po.issued_date}",
                )
            result.check(
                "po_date",
                "Invoice dated after the PO",
                after,
                f"Invoice {invoice.invoice_date}, PO issued {po.issued_date}",
            )

        all_lines_matched = await self._match_lines(invoice, invoice_id, po, result)
        if not all_lines_matched:
            await self._match_totals(invoice, invoice_id, po, result)

    async def _match_lines(
        self,
        invoice: ExtractedInvoice,
        invoice_id: uuid.UUID,
        po: PurchaseOrder,
        result: MatchResult,
    ) -> bool:
        """Line-by-line 3-way match. True when every invoice line was paired and checked."""
        lines = [(index, line) for index, line in enumerate(invoice.line_items) if line.amount]
        if not lines or not po.lines:
            return False
        received: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
        for receipt in po.receipts:
            for receipt_line in receipt.lines:
                received[receipt_line.purchase_order_line_id] += receipt_line.quantity_received
        billed = await self._already_billed_quantities(po, invoice_id)

        unmatched, price_issues, quantity_issues = [], [], []
        checked_all = True
        for index, line in lines:
            po_line = _pair_line(line, po.lines, single=len(lines) == 1)
            if po_line is None:
                unmatched.append(line)
                result.add(
                    ReasonCode.LINE_NOT_ON_PO,
                    f"'{line.description}' ({line.amount:,.2f}) matches no line of {po.po_number}",
                )
                continue
            result.line_matches[index] = po_line.id
            quantity = line.quantity
            price = line.unit_price
            if price is None and quantity and line.amount is not None:
                price = (line.amount / quantity).quantize(_CENT)
            if quantity is None or price is None:
                checked_all = False
                continue

            over = ((price - po_line.unit_price) * quantity).quantize(_CENT)
            if over > self.allowed(po_line.unit_price * quantity):
                price_issues.append(line.description)
                result.add(
                    ReasonCode.PRICE_VARIANCE,
                    f"'{line.description}': billed {price:,.2f} each, PO price"
                    f" {po_line.unit_price:,.2f} ({over:,.2f} over for {_qty(quantity)})",
                )

            billed[po_line.id] += quantity
            total_billed = billed[po_line.id]
            if po.needs_receiving and total_billed > received[po_line.id]:
                quantity_issues.append(line.description)
                result.add(
                    ReasonCode.QUANTITY_VARIANCE,
                    f"'{line.description}': {_qty(total_billed)} billed in total, only"
                    f" {_qty(received[po_line.id])} received",
                )
            elif total_billed > po_line.quantity:
                quantity_issues.append(line.description)
                result.add(
                    ReasonCode.QUANTITY_VARIANCE,
                    f"'{line.description}': {_qty(total_billed)} billed in total, only"
                    f" {_qty(po_line.quantity)} ordered",
                )

        paired = len(lines) - len(unmatched)
        result.check(
            "lines_on_po",
            "Every line is on the PO",
            not unmatched,
            f"{paired} of {len(lines)} lines matched {po.po_number}",
        )
        result.check(
            "line_prices",
            "Line prices match the PO",
            not price_issues,
            ", ".join(price_issues) + " above the PO price"
            if price_issues
            else f"Within {self.tolerance_text}",
        )
        if po.needs_receiving:
            result.check(
                "line_received",
                "Every line was delivered",
                not quantity_issues,
                ", ".join(quantity_issues) + ": more billed than delivered or ordered"
                if quantity_issues
                else "Billed quantities are within what was delivered",
            )
        else:
            result.check(
                "line_received",
                "Billed within what was ordered",
                not quantity_issues,
                ", ".join(quantity_issues) + ": more billed than ordered"
                if quantity_issues
                else f"Two-way match: {po.po_number} needs no receiving",
            )
        return not unmatched and checked_all

    async def _match_totals(
        self,
        invoice: ExtractedInvoice,
        invoice_id: uuid.UUID,
        po: PurchaseOrder,
        result: MatchResult,
    ) -> None:
        """The fallback when lines cannot be paired: compare billed totals."""
        net = _net_amount(invoice.subtotal, invoice.discount, invoice.total, invoice.tax)
        if net is None:
            result.check("po_amount", "Within what was ordered", None, "No amount to compare")
            return
        already = await self._already_invoiced(po.id, invoice_id)
        claimed = already + net

        within_order = claimed <= po.total_amount + self.allowed(po.total_amount)
        if not within_order:
            result.add(
                ReasonCode.PO_AMOUNT_EXCEEDED,
                f"{po.po_number} is for {po.total_amount}; with this invoice"
                f" {claimed} would be billed",
            )
        result.check(
            "po_amount",
            "Within what was ordered",
            within_order,
            f"Billed {claimed:,.2f} (this invoice {net:,.2f} + earlier {already:,.2f})"
            f" of {po.total_amount:,.2f} ordered",
        )
        if not po.needs_receiving:
            result.check(
                "received",
                "Delivered before being billed",
                None,
                f"Not needed: {po.po_number} is matched to the order only",
            )
            return
        received = sum(
            (
                line.quantity_received * line.purchase_order_line.unit_price
                for receipt in po.receipts
                for line in receipt.lines
            ),
            Decimal(0),
        ).quantize(_CENT)
        delivered = claimed <= received + self.allowed(received)
        if not delivered:
            result.add(
                ReasonCode.GOODS_NOT_RECEIVED,
                f"Only {received} of {po.po_number} has been received; with this invoice"
                f" {claimed} would be billed",
            )
        result.check(
            "received",
            "Delivered before being billed",
            delivered,
            f"Received {received:,.2f}; billed {claimed:,.2f}",
        )

    async def _already_billed_quantities(
        self, po: PurchaseOrder, invoice_id: uuid.UUID
    ) -> dict[uuid.UUID, Decimal]:
        rows = await self._session.execute(
            select(InvoiceLine.purchase_order_line_id, func.sum(InvoiceLine.quantity))
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(
                InvoiceLine.purchase_order_line_id.in_([line.id for line in po.lines]),
                Invoice.id != invoice_id,
                Invoice.status.not_in(_INACTIVE_STATUSES),
            )
            .group_by(InvoiceLine.purchase_order_line_id)
        )
        billed: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
        for line_id, quantity in rows:
            if line_id is not None and quantity is not None:
                billed[line_id] = Decimal(quantity)
        return billed

    async def _already_invoiced(self, po_id: uuid.UUID, invoice_id: uuid.UUID) -> Decimal:
        rows = await self._session.execute(
            select(Invoice.subtotal, Invoice.discount, Invoice.total, Invoice.tax_total).where(
                Invoice.purchase_order_id == po_id,
                Invoice.id != invoice_id,
                Invoice.status.not_in(_INACTIVE_STATUSES),
            )
        )
        return sum(
            (_net_amount(*row) or Decimal(0) for row in rows),
            Decimal(0),
        )


def _check_sender(vendor: Vendor, sender_email: str | None, result: MatchResult) -> None:
    """An emailed invoice should come from the vendor's own domain (or a subdomain of it).

    Checked only when both are known: a missing sender or domain is not a problem.
    """
    if not sender_email or "@" not in sender_email or not vendor.email_domain:
        return
    sender = sender_email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    expected = vendor.email_domain.strip().lower().lstrip("@").rstrip(".")
    ours = sender == expected or sender.endswith("." + expected)
    label = "Sent from the vendor's email domain"
    if ours:
        result.check("sender", label, True, f"Sent from {sender}")
        return
    lookalike = SequenceMatcher(None, sender, expected).ratio() >= _LOOKALIKE_RATIO
    how = (
        f"{sender} looks like {expected} but is not it"
        if lookalike
        else f"{sender}, not {expected}"
    )
    result.add(
        ReasonCode.SENDER_DOMAIN_MISMATCH,
        f"Emailed from {sender_email}: {how}, the domain on file for {vendor.name}",
    )
    result.check("sender", label, False, how)


def _check_tax_id(vendor: Vendor, printed: str | None, result: MatchResult) -> None:
    """The tax ID printed on the invoice must be the vendor's (formatting ignored).

    Checked only when both are known: an invoice without a tax ID is not flagged.
    """
    if not printed or not vendor.tax_id:
        return
    label = "Tax ID matches our records"
    if _alnum(printed) == _alnum(vendor.tax_id):
        result.check("tax_id", label, True, f"{vendor.tax_id}")
        return
    result.add(
        ReasonCode.TAX_ID_MISMATCH,
        f"The invoice shows tax ID {printed}; {vendor.name} is on file with {vendor.tax_id}",
    )
    result.check("tax_id", label, False, f"Invoice {printed}, on file {vendor.tax_id}")


def _alnum(value: str) -> str:
    return _NOT_ALNUM.sub("", value.upper())


def _words(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOP_WORDS}


def _similarity(a: str, b: str) -> float:
    left, right = _words(a), _words(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _pair_line(
    line: ExtractedLine, po_lines: list[PurchaseOrderLine], *, single: bool
) -> PurchaseOrderLine | None:
    """The PO line an invoice line bills for: closest description, else a unique price.

    A one-line invoice against a one-line PO pairs when they share any word, so the same
    service worded differently still pairs ("IT support, Q4" against "IT support
    retainer, October to December").
    """
    best = max(po_lines, key=lambda po_line: _similarity(line.description, po_line.description))
    if _similarity(line.description, best.description) >= _LINE_SIMILARITY:
        return best
    if line.unit_price is not None:
        same_price = [p for p in po_lines if abs(p.unit_price - line.unit_price) < _CENT]
        if len(same_price) == 1:
            return same_price[0]
    # One line against one line: pair if they share any word (the same service, worded
    # differently), never two unrelated items.
    if single and len(po_lines) == 1 and _similarity(line.description, po_lines[0].description):
        return po_lines[0]
    return None


# Characters that scanning and retyping confuse: O/0, I/L/1, S/5, B/8, Z/2.
_LOOKALIKES = str.maketrans("OILSBZ", "011582")


def _similar_numbers(a: str, b: str) -> bool:
    """The same invoice number, misread or re-sent: not merely the next number.

    Similar means equal once lookalike characters are folded ("AS-2O431" and "AS-20431"),
    or one with a short suffix or prefix added ("AS-20431" and "AS-20431-R"). Numbers that
    differ by a real digit ("AS-20431" and "AS-20432") are a vendor's consecutive invoices.
    """
    if a == b or min(len(a), len(b)) < 4:
        return False
    left, right = a.translate(_LOOKALIKES), b.translate(_LOOKALIKES)
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(longer) - len(shorter) <= 3 and shorter in longer


def _qty(value: Decimal) -> str:
    """12.0000 -> '12', 2.5000 -> '2.5'."""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _net_amount(
    subtotal: Decimal | None,
    discount: Decimal | None,
    total: Decimal | None,
    tax: Decimal | None,
) -> Decimal | None:
    """Pre-tax amount actually billed: subtotal - discount, else total - tax."""
    if subtotal is not None:
        return subtotal - (discount or 0)
    if total is not None:
        return total - (tax or 0)
    return None
