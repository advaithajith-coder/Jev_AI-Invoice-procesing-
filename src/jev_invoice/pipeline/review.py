"""Human review: list open tasks, approve or reject them, with an audit trail."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jev_invoice.core import audit
from jev_invoice.core.errors import AppError, ConflictError, NotFoundError
from jev_invoice.db.models import Invoice, ReviewTask
from jev_invoice.domain.enums import ActionBand, InvoiceStatus, ReviewStatus


class NoteRequiredError(AppError):
    status_code = 422
    code = "note_required"


def review_query() -> Select[ReviewTask]:
    invoice = selectinload(ReviewTask.invoice)
    return select(ReviewTask).options(
        selectinload(ReviewTask.decision),
        invoice.selectinload(Invoice.lines),
        invoice.selectinload(Invoice.vendor),
        invoice.selectinload(Invoice.decisions),
        invoice.selectinload(Invoice.purchase_order),
    )


class ReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_open(self, *, limit: int = 50) -> list[ReviewTask]:
        result = await self._session.scalars(
            review_query()
            .where(ReviewTask.status == ReviewStatus.OPEN)
            .order_by(ReviewTask.created_at)
            .limit(limit)
        )
        return list(result)

    async def get(self, task_id: uuid.UUID) -> ReviewTask:
        task = await self._session.scalar(review_query().where(ReviewTask.id == task_id))
        if task is None:
            raise NotFoundError("Review task not found", details={"task_id": str(task_id)})
        return task

    async def resolve(
        self,
        task_id: uuid.UUID,
        *,
        approve: bool,
        reviewer: str,
        note: str | None,
        warnings: list[str] | None = None,
    ) -> ReviewTask:
        """Approve or reject an open task and move its invoice to the matching status.

        *warnings* are controls a real company would enforce (role limits, a second
        approver, separation of duties). This demo only warns, and records that it did.
        """
        task = await self.get(task_id)
        if task.status != ReviewStatus.OPEN:
            raise ConflictError(
                f"Review task is already {task.status}", details={"task_id": str(task_id)}
            )
        note = note.strip() if note else None
        if approve and task.decision.band == ActionBand.BLOCK and not note:
            raise NoteRequiredError(
                "This invoice was blocked as possible fraud. Approving it needs a note saying"
                " how it was verified (for example, a call to the vendor on a known number).",
                details={"task_id": str(task_id)},
            )
        task.status = ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED
        task.resolved_by = reviewer
        task.resolved_at = datetime.now(UTC)
        task.resolution_note = note
        task.invoice.status = InvoiceStatus.APPROVED if approve else InvoiceStatus.REJECTED
        audit.record(
            self._session,
            action="review.approved" if approve else "review.rejected",
            entity_type="review_task",
            entity_id=task.id,
            actor=reviewer,
            data={
                "invoice_id": str(task.invoice_id),
                "proposed_action": task.decision.action,
                "note": note,
                **({"warnings": warnings} if warnings else {}),
            },
        )
        await self._session.commit()
        return task
