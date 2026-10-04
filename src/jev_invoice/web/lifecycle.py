"""Where an invoice is in its life: received, checked, approved, exported, paid.

Drawn as a small rail of five steps on lists and a labelled one on the invoice page.
The step the invoice is waiting at carries a tone: blue while work goes on, amber when
a person is needed or it is held, red when it stopped, green once it is paid.
"""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import inspect

from jev_invoice.db.models import Invoice
from jev_invoice.domain.enums import DocumentType, InvoiceStatus
from jev_invoice.payables.service import invoice_balance

STEPS = ("Received", "Checked", "Approved", "Exported", "Paid")

Tone = Literal["info", "warn", "bad", "good"]
StepState = Literal["done", "current", "todo"]


@dataclass(frozen=True)
class Rail:
    done: int
    """How many steps are complete, from the first."""
    tone: Tone
    caption: str
    finished: bool = False
    """No step is waiting: the invoice is paid, or a credit note is applied."""

    @property
    def steps(self) -> list[tuple[str, StepState]]:
        states: list[tuple[str, StepState]] = []
        for index, name in enumerate(STEPS):
            if index < self.done:
                states.append((name, "done"))
            elif index == self.done and not self.finished:
                states.append((name, "current"))
            else:
                states.append((name, "todo"))
        return states

    @property
    def label(self) -> str:
        """For screen readers: the rail as one sentence."""
        reached = ", ".join(name.lower() for name, state in self.steps if state == "done")
        return f"{reached.capitalize() or 'Not started'}. {self.caption}."


def _payment_state(invoice: Invoice) -> str | None:
    """unpaid, part_paid or paid; None when payments were not loaded with the invoice."""
    unloaded = inspect(invoice).unloaded
    if "payments" in unloaded or "credits" in unloaded:
        return None
    return invoice_balance(invoice).state


def lifecycle(invoice: Invoice) -> Rail:
    status = invoice.status
    held = {"dispute": "Disputed", "hold": "On hold"}.get(invoice.hold_kind or "")

    if invoice.document_type == DocumentType.CREDIT_NOTE and status == InvoiceStatus.APPROVED:
        return Rail(3, "good", "Credit applied", finished=True)
    if status in (InvoiceStatus.RECEIVED, InvoiceStatus.EXTRACTED):
        return Rail(1, "info", "Being checked")
    if status == InvoiceStatus.FAILED:
        return Rail(1, "bad", "Could not be read")
    if status == InvoiceStatus.PENDING_REVIEW:
        return Rail(2, "warn", held or "Waiting for review")
    if status == InvoiceStatus.REJECTED:
        return Rail(2, "bad", "Rejected")

    paid = _payment_state(invoice)
    if paid == "paid":
        return Rail(5, "good", "Paid", finished=True)
    exported = status == InvoiceStatus.EXPORTED
    done = 4 if exported else 3
    if held:
        return Rail(done, "warn", held)
    if paid == "part_paid":
        return Rail(done, "info", "Part paid")
    return Rail(done, "info", "Awaiting payment" if exported else "Ready to export")
