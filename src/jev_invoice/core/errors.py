"""Application error hierarchy. Every API error is rendered as {"error": {code, message, ...}}."""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class DependencyUnavailableError(AppError):
    """An upstream service (database, Jev, LLM provider) cannot be reached."""

    status_code = 503
    code = "dependency_unavailable"


class ExtractionError(AppError):
    """The extraction model failed or returned something unusable."""

    status_code = 502
    code = "extraction_failed"


class BudgetExceededError(AppError):
    """The daily spend limit for an external model has been reached."""

    status_code = 429
    code = "budget_exceeded"


class DecisionError(AppError):
    """Jev returned no usable answers. The decision service fails closed on this."""

    status_code = 503
    code = "decision_unavailable"


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


async def _handle_app_error(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # registered for AppError only; keeps mypy narrow
        raise exc
    return JSONResponse(
        status_code=exc.status_code, content=error_body(exc.code, exc.message, exc.details)
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
