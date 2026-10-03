"""Liveness (process is up) and readiness (dependencies reachable) probes."""

import asyncio
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from jev_invoice import __version__
from jev_invoice.api.dependencies import ReadinessCheck, get_readiness_checks

router = APIRouter(prefix="/health", tags=["health"])
log = structlog.get_logger(__name__)

_CHECK_TIMEOUT_SECONDS = 3.0


class LiveResponse(BaseModel):
    status: Literal["ok"]
    version: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, str]


@router.get("/live")
async def live() -> LiveResponse:
    return LiveResponse(status="ok", version=__version__)


@router.get("/ready", responses={503: {"model": ReadyResponse}})
async def ready(
    response: Response,
    checks: Annotated[dict[str, ReadinessCheck], Depends(get_readiness_checks)],
) -> ReadyResponse:
    names = list(checks)
    outcomes = await asyncio.gather(
        *(asyncio.wait_for(checks[name](), _CHECK_TIMEOUT_SECONDS) for name in names),
        return_exceptions=True,
    )
    results: dict[str, str] = {}
    for name, outcome in zip(names, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            # Report the exception type only; messages can carry hostnames or credentials.
            results[name] = f"error: {type(outcome).__name__}"
            log.warning("readiness_check_failed", check=name, error=repr(outcome))
        else:
            results[name] = "ok"

    is_ready = all(value == "ok" for value in results.values())
    if not is_ready:
        response.status_code = 503
    return ReadyResponse(status="ready" if is_ready else "not_ready", checks=results)
