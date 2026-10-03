"""arq worker. Run with `uv run poe worker`. Pipeline jobs are registered in later milestones."""

from typing import Any, ClassVar

import structlog
from arq.connections import RedisSettings

from jev_invoice.config import get_settings
from jev_invoice.core.logging import configure_logging
from jev_invoice.db.session import create_engine, create_session_factory

log = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    engine = create_engine(settings)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    log.info("worker_startup", env=settings.app_env)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()


async def ping(_ctx: dict[str, Any]) -> str:
    """Smoke-test job: proves the API can enqueue and the worker can run."""
    return "pong"


class WorkerSettings:
    functions: ClassVar[list[Any]] = [ping]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url or "redis://localhost:6379/0")
    max_jobs = 10
    job_timeout = 300
    keep_result = 3600
