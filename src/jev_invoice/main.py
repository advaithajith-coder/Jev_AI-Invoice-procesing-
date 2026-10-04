"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import groq
import httpx
import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from jev_invoice import __version__
from jev_invoice.api.dependencies import build_pipeline
from jev_invoice.api.middleware import RequestContextMiddleware
from jev_invoice.api.routes import exports, health, invoices, masterdata, review
from jev_invoice.config import Settings, get_settings
from jev_invoice.core.errors import register_error_handlers
from jev_invoice.core.logging import configure_logging
from jev_invoice.db.session import create_engine, create_session_factory
from jev_invoice.decision.jev_client import JevClient
from jev_invoice.decision.service import DecisionService
from jev_invoice.extraction.client import LLMClient
from jev_invoice.extraction.providers import GroqProvider, OllamaProvider
from jev_invoice.extraction.service import ExtractionService
from jev_invoice.extraction.vision import OCR_STOP_SEQUENCES, TranscriptionService
from jev_invoice.fx.service import FrankfurterSource
from jev_invoice.ingest.storage import LocalFileStorage
from jev_invoice.pipeline.runner import BackgroundRunner
from jev_invoice.web import admin as web_admin
from jev_invoice.web import exceptions as web_exceptions
from jev_invoice.web import journey as web_journey
from jev_invoice.web import payables as web_payables
from jev_invoice.web import routes as web
from jev_invoice.web import workflow as web_workflow

log = structlog.get_logger(__name__)

WEB_STATIC_DIR = Path(web.__file__).parent / "static"


def build_extraction(
    settings: Settings,
) -> tuple[ExtractionService | None, groq.AsyncGroq | None]:
    """The extraction service and its Groq SDK, or None when not configured."""
    key = settings.groq_api_key.get_secret_value().strip() if settings.groq_api_key else ""
    if not key or key == "test-key":
        log.warning("extraction_not_configured")
        return None, None
    # SDK retries off: LLMClient owns retry, backoff, and the circuit breaker.
    sdk = groq.AsyncGroq(api_key=key, max_retries=0, timeout=settings.llm_timeout_seconds)
    return ExtractionService(LLMClient(GroqProvider(sdk, settings.groq_model), settings)), sdk


def build_transcription(
    settings: Settings,
) -> tuple[TranscriptionService | None, httpx.AsyncClient | None]:
    """Vision transcription with a local Ollama model, or None when not configured.

    Scans and photos are read only on this machine: page images are never sent to a
    cloud model.
    """
    if not settings.vision_model:
        return None, None
    http = httpx.AsyncClient(
        base_url=settings.ollama_base_url, timeout=settings.ollama_timeout_seconds
    )
    ocr_model = "ocr" in settings.vision_model.lower()
    provider = OllamaProvider(
        http,
        settings.vision_model,
        num_ctx=settings.ollama_num_ctx,
        max_output_tokens=settings.ollama_max_output_tokens,
        stop=OCR_STOP_SEQUENCES if ocr_model else (),
    )
    # Local and free: no cost per token.
    client = LLMClient(
        provider, settings, cost_per_input_token=Decimal(0), cost_per_output_token=Decimal(0)
    )
    return TranscriptionService(client, ocr_model=ocr_model), http


def build_rates(settings: Settings) -> tuple[FrankfurterSource | None, httpx.AsyncClient | None]:
    """Exchange rates for invoices in another currency, or None when switched off."""
    if not settings.fx_rates_url:
        return None, None
    http = httpx.AsyncClient(timeout=settings.fx_timeout_seconds)
    return FrankfurterSource(http, settings.fx_rates_url), http


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        extraction, groq_sdk = build_extraction(settings)
        transcription, ollama_http = build_transcription(settings)
        rates, rates_http = build_rates(settings)
        jev = JevClient.from_settings(settings)

        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.redis = Redis.from_url(settings.redis_url) if settings.redis_url else None
        app.state.storage = LocalFileStorage(settings.storage_dir)
        app.state.extraction = extraction
        app.state.transcription = transcription
        app.state.rates = rates
        app.state.decisions = DecisionService(jev, settings)
        app.state.runner = None
        if settings.background_processing:
            runner = BackgroundRunner(
                app.state.session_factory, lambda session: build_pipeline(app.state, session)
            )
            app.state.runner = runner
            await runner.resume_pending()
        log.info(
            "startup",
            env=settings.app_env,
            version=__version__,
            extraction=settings.groq_model if extraction else "not configured",
            vision=f"ollama:{settings.vision_model}" if transcription else "not configured",
            jev=settings.typesafe_model if jev else "not configured",
            jev_base_url=settings.typesafe_base_url or "typesafe",
        )
        try:
            yield
        finally:
            if app.state.runner is not None:
                await app.state.runner.shutdown()
            if jev is not None:
                await jev.aclose()
            if groq_sdk is not None:
                await groq_sdk.close()
            if ollama_http is not None:
                await ollama_http.aclose()
            if rates_http is not None:
                await rates_http.aclose()
            if app.state.redis is not None:
                await app.state.redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Jev Invoice",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,  # no API screen in the app; the JSON API stays for integrations
        redoc_url=None,
    )
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(invoices.router)
    app.include_router(review.router)
    app.include_router(masterdata.router)
    app.include_router(exports.router)
    app.include_router(web.router)
    app.include_router(web_admin.router)
    app.include_router(web_journey.router)
    app.include_router(web_payables.router)
    app.include_router(web_exceptions.router)
    app.include_router(web_workflow.router)
    app.mount("/static", StaticFiles(directory=WEB_STATIC_DIR), name="static")
    web.use_company(settings.company_name, settings.base_currency)
    return app


app = create_app()
