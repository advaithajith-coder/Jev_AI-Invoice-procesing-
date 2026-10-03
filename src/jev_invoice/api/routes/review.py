"""Human review queue."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from jev_invoice.api.dependencies import get_review_service
from jev_invoice.api.schemas import ResolveReviewIn, ReviewTaskOut, review_task_out
from jev_invoice.pipeline.review import ReviewService

router = APIRouter(prefix="/api/v1/review", tags=["review"])
ReviewDep = Annotated[ReviewService, Depends(get_review_service)]


@router.get("")
async def list_open_reviews(
    service: ReviewDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[ReviewTaskOut]:
    """Invoices waiting for a person, oldest first, with the reasons they need one."""
    return [review_task_out(task) for task in await service.list_open(limit=limit)]


@router.post("/{task_id}/approve")
async def approve(task_id: uuid.UUID, body: ResolveReviewIn, service: ReviewDep) -> ReviewTaskOut:
    task = await service.resolve(task_id, approve=True, reviewer=body.reviewer, note=body.note)
    return review_task_out(await service.get(task.id))


@router.post("/{task_id}/reject")
async def reject(task_id: uuid.UUID, body: ResolveReviewIn, service: ReviewDep) -> ReviewTaskOut:
    task = await service.resolve(task_id, approve=False, reviewer=body.reviewer, note=body.note)
    return review_task_out(await service.get(task.id))
