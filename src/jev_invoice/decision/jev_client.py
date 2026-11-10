"""Async wrapper around the TypeSafe SDK for Jev System One calls.

Works with TypeSafe directly or with any gateway that serves the same API (BeatAPI:
set TYPESAFE_BASE_URL=https://api.beatapi.io). Adds a circuit breaker, cost and
latency logging, and one exception type (DecisionError) for every failure. Retries
with backoff come from the SDK's RetryPolicy, bounded by a total time budget.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

import structlog
from typesafe_sdk import (
    AsyncTypeSafeClient,
    JSONContent,
    Question,
    RetryPolicy,
    SystemOneResponse,
    TypeSafeError,
)

from jev_invoice.config import Settings
from jev_invoice.core.errors import DecisionError
from jev_invoice.core.resilience import CircuitBreaker
from jev_invoice.evaluation.telemetry import record_call

log = structlog.get_logger(__name__)

_PLACEHOLDER_KEYS = {"", "test-key"}


@dataclass(frozen=True)
class JevResult:
    response: SystemOneResponse
    model: str
    input_tokens: int
    cost_usd: Decimal
    latency_ms: float
    waited_seconds: float = 0.0


class JevClient:
    def __init__(
        self,
        sdk_client: AsyncTypeSafeClient,
        settings: Settings,
        *,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sdk = sdk_client
        self._settings = settings
        self._sleep = sleep
        self._clock = clock
        self._pace = asyncio.Lock()
        self._last_call_end: float | None = None
        self._breaker = breaker or CircuitBreaker(
            "Jev",
            failure_threshold=settings.circuit_breaker_threshold,
            reset_seconds=settings.circuit_breaker_reset_seconds,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "JevClient | None":
        """Build a client, or return None when no Jev key is configured.

        None (instead of an error) lets the app start without a key; the decision
        service then routes every invoice to human review.
        """
        secret = settings.typesafe_api_key
        api_key = secret.get_secret_value().strip() if secret else ""
        if api_key in _PLACEHOLDER_KEYS:
            log.warning("jev_not_configured")
            return None
        try:
            sdk = AsyncTypeSafeClient(
                api_key=api_key,
                base_url=settings.typesafe_base_url or None,
                model=settings.typesafe_model,
                timeout=settings.typesafe_timeout_seconds,
                retry=RetryPolicy(max_retries=2, timeout=settings.typesafe_timeout_seconds),
            )
        except TypeSafeError as exc:
            log.error("jev_invalid_configuration", error=type(exc).__name__)
            return None
        return cls(sdk, settings)

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def expected_wait_seconds(self) -> float:
        """How long the next call would wait for the minimum interval (0 if none)."""
        interval = self._settings.jev_min_interval_seconds
        if not interval or self._last_call_end is None:
            return 0.0
        return max(0.0, interval - (self._clock() - self._last_call_end))

    async def ask(self, state: JSONContent, questions: Mapping[str, Question]) -> JevResult:
        """Ask Jev a set of closed questions about one state.

        Raises:
            CircuitOpenError: too many recent failures; calls are paused.
            DecisionError: the call failed after the SDK's retries.
        """
        queued = self._clock()
        async with self._pace:  # one call at a time, spaced by jev_min_interval_seconds
            waited = self.expected_wait_seconds()
            if waited > 0:
                log.info("jev_waiting_for_rate_limit", seconds=round(waited, 1))
                await self._sleep(waited)
            try:
                return await self._call(state, questions, self._clock() - queued)
            finally:
                self._last_call_end = self._clock()

    async def _call(
        self, state: JSONContent, questions: Mapping[str, Question], waited: float
    ) -> JevResult:
        self._breaker.before_call()
        start = time.monotonic()
        first = next(iter(questions), "")
        stage = (
            "tax"
            if first.startswith("tax_")
            else "classification"
            if first.startswith("finance_")
            else "gl_coding"
            if first.startswith("line_")
            else "routing"
            if first.startswith("route_")
            else "judgment"
        )
        try:
            response = await self._sdk.system_one(state, questions)
        except TypeSafeError as exc:
            record_call(
                stage=stage,
                provider="jev",
                model=self._settings.typesafe_model,
                status="failed",
                latency_ms=round((time.monotonic() - start) * 1000, 1),
                wait_ms=round(waited * 1000, 1),
                cost_usd=None,
                cost_basis="unknown",
            )
            self._breaker.record_failure()
            status = getattr(exc, "status", None)
            log.warning("jev_call_failed", error=type(exc).__name__, http_status=status)
            raise DecisionError(
                f"Jev call failed ({type(exc).__name__}).",
                details={"error_type": type(exc).__name__, "http_status": status},
            ) from exc
        self._breaker.record_success()

        input_tokens = response.usage.input_tokens or 0
        result = JevResult(
            response=response,
            model=response.model,
            input_tokens=input_tokens,
            cost_usd=input_tokens * self._settings.jev_cost_per_input_token_usd,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            waited_seconds=round(waited, 1),
        )
        record_call(
            stage=stage,
            provider="jev",
            model=result.model,
            status="success",
            latency_ms=result.latency_ms,
            wait_ms=round(waited * 1000, 1),
            input_tokens=result.input_tokens,
            output_tokens=None,
            cost_usd=str(result.cost_usd),
            cost_basis="estimated",
        )
        log.info(
            "jev_call",
            model=result.model,
            questions=len(questions),
            input_tokens=input_tokens,
            latency_ms=result.latency_ms,
        )
        return result

    async def aclose(self) -> None:
        await self._sdk.aclose()
