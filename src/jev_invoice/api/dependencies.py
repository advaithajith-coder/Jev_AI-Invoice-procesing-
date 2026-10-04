"""FastAPI dependency providers. Tests replace these via `app.dependency_overrides`."""

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from jev_invoice.config import Settings
from jev_invoice.export.service import ExportService
from jev_invoice.masterdata.importing import CsvImporter
from jev_invoice.masterdata.purchase_orders import PurchaseOrderService
from jev_invoice.masterdata.vendors import VendorService
from jev_invoice.pipeline.review import ReviewService
from jev_invoice.pipeline.service import InvoicePipeline

ReadinessCheck = Callable[[], Awaitable[None]]


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def build_pipeline(state: Any, session: AsyncSession) -> InvoicePipeline:
    """A pipeline wired to the app's services (reads them at call time, so tests can swap)."""
    return InvoicePipeline(
        session,
        state.settings,
        storage=state.storage,
        extraction=state.extraction,
        decisions=state.decisions,
        transcription=state.transcription,
        rates=getattr(state, "rates", None),
    )


def get_pipeline(request: Request, session: SessionDep) -> InvoicePipeline:
    return build_pipeline(request.app.state, session)


def get_review_service(session: SessionDep) -> ReviewService:
    return ReviewService(session)


def get_vendor_service(request: Request, session: SessionDep) -> VendorService:
    settings: Settings = request.app.state.settings
    return VendorService(
        session, fingerprint_key=settings.bank_fingerprint_key.get_secret_value().encode()
    )


def get_po_service(session: SessionDep) -> PurchaseOrderService:
    return PurchaseOrderService(session)


def get_importer(request: Request, session: SessionDep) -> CsvImporter:
    return CsvImporter(session, get_vendor_service(request, session), PurchaseOrderService(session))


def get_export_service(request: Request, session: SessionDep) -> ExportService:
    return ExportService(session, request.app.state.settings)


def get_readiness_checks(request: Request) -> dict[str, ReadinessCheck]:
    engine: AsyncEngine = request.app.state.engine
    redis: Redis | None = request.app.state.redis

    async def database() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    checks: dict[str, ReadinessCheck] = {"database": database}
    if redis is not None:

        async def queue() -> None:
            await redis.ping()

        checks["queue"] = queue
    return checks
