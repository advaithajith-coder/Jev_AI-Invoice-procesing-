"""Run invoice processing in the background, so uploads return straight away.

Each job gets its own database session. Jobs are plain asyncio tasks in the web process,
which is right for one server; with several servers this becomes the arq worker. On
startup, invoices left in "received" (for example by a restart mid-processing) are
picked up again; processing is idempotent because it only starts from "received".
"""

import asyncio
import uuid
from collections.abc import Callable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jev_invoice.db.models import Invoice
from jev_invoice.pipeline.service import IN_PROGRESS, InvoicePipeline

log = structlog.get_logger(__name__)


class BackgroundRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        build_pipeline: Callable[[AsyncSession], InvoicePipeline],
    ) -> None:
        self._session_factory = session_factory
        self._build = build_pipeline
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def active(self) -> int:
        return len(self._tasks)

    def submit(self, invoice_id: uuid.UUID) -> None:
        task = asyncio.create_task(self._run(invoice_id), name=f"invoice-{invoice_id}")
        self._tasks.add(task)  # keep a reference so the task is not garbage-collected
        task.add_done_callback(self._tasks.discard)

    async def resume_pending(self) -> int:
        async with self._session_factory() as session:
            ids = list(
                await session.scalars(select(Invoice.id).where(Invoice.status.in_(IN_PROGRESS)))
            )
        for invoice_id in ids:
            self.submit(invoice_id)
        if ids:
            log.info("resumed_pending_invoices", count=len(ids))
        return len(ids)

    async def wait_idle(self) -> None:
        """Wait for every submitted job to finish (used by tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self, grace_seconds: float = 10.0) -> None:
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(list(self._tasks), timeout=grace_seconds)
        for task in pending:
            task.cancel()  # resumed on next startup: the invoice stays "received"

    async def _run(self, invoice_id: uuid.UUID) -> None:
        structlog.contextvars.bind_contextvars(invoice_id=str(invoice_id))
        try:
            async with self._session_factory() as session:
                await self._build(session).process(invoice_id)
        except Exception:
            log.exception("background_processing_failed")
