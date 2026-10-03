"""Record each pipeline step as it happens, so screens can follow an invoice live.

    async with recorder.run("extract") as step:
        ...
        step.summary = "Read 8 fields with openai/gpt-oss-120b"
        step.details = {...}

The step is saved as "running" when it starts and "done" or "failed" when it ends,
each with a commit, so another request can see progress while processing continues.
"""

import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.core.errors import AppError
from jev_invoice.db.models import PipelineStep

STEPS: dict[str, tuple[int, str]] = {
    "received": (1, "Received"),
    "read": (2, "Read the file"),
    "transcribe": (3, "Vision transcription"),
    "extract": (4, "Extract fields (LLM)"),
    "document_checks": (5, "Document checks"),
    "master_data": (6, "Vendor and PO checks"),
    "judgment": (7, "Jev judgment"),
    "decision": (8, "Decision"),
    "outcome": (9, "Outcome"),
}


def jsonable(value: Any) -> Any:
    """Decimals, dates and UUIDs to strings, so details can be stored as JSON."""
    return json.loads(json.dumps(value, default=str))


@dataclass
class StepHandle:
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    status: str | None = None  # override "done", e.g. "warning"
    _row: PipelineStep | None = field(default=None, repr=False)
    _session: AsyncSession | None = field(default=None, repr=False)

    async def progress(self, message: str) -> None:
        """Show a message on the running step straight away (e.g. waiting for a rate limit)."""
        if self._row is not None and self._session is not None:
            self._row.summary = message
            await self._session.commit()


class StepRecorder:
    def __init__(self, session: AsyncSession, invoice_id: uuid.UUID) -> None:
        self._session = session
        self._invoice_id = invoice_id

    @asynccontextmanager
    async def run(self, step: str) -> AsyncIterator[StepHandle]:
        row = PipelineStep(
            id=uuid.uuid4(),
            invoice_id=self._invoice_id,
            step=step,
            position=STEPS[step][0],
            status="running",
            started_at=datetime.now(UTC),
            summary="",
            details={},
        )
        self._session.add(row)
        await self._session.commit()
        start = time.perf_counter()
        handle = StepHandle(_row=row, _session=self._session)
        try:
            yield handle
        except Exception as exc:
            row.status = "failed"
            row.summary = handle.summary or (
                exc.message if isinstance(exc, AppError) else "Unexpected error"
            )
            self._finish(row, handle, start)
            await self._session.commit()
            raise
        row.status = handle.status or "done"
        row.summary = handle.summary
        self._finish(row, handle, start)
        await self._session.commit()

    async def skip(self, step: str, summary: str) -> None:
        now = datetime.now(UTC)
        self._session.add(
            PipelineStep(
                id=uuid.uuid4(),
                invoice_id=self._invoice_id,
                step=step,
                position=STEPS[step][0],
                status="skipped",
                started_at=now,
                finished_at=now,
                duration_ms=0.0,
                summary=summary,
                details={},
            )
        )
        await self._session.commit()

    @staticmethod
    def _finish(row: PipelineStep, handle: StepHandle, start: float) -> None:
        row.details = jsonable(handle.details)
        row.finished_at = datetime.now(UTC)
        row.duration_ms = round((time.perf_counter() - start) * 1000, 1)
