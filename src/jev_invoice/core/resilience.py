"""Circuit breaker and retry-with-backoff for calls to external models.

CircuitBreaker states:
    closed     calls flow; consecutive failures are counted
    open       calls are refused immediately until `reset_seconds` pass
    half_open  one trial call is allowed; success closes, failure re-opens
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog

from jev_invoice.core.errors import DependencyUnavailableError

log = structlog.get_logger(__name__)

BreakerState = Literal["closed", "open", "half_open"]


class CircuitOpenError(DependencyUnavailableError):
    code = "circuit_open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int,
        reset_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._reset_seconds:
            return "half_open"
        return "open"

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def before_call(self) -> None:
        """Raise CircuitOpenError if the call must not be attempted."""
        state = self.state
        if state == "open" or (state == "half_open" and self._trial_in_flight):
            assert self._opened_at is not None  # noqa: S101 - narrowed by state
            retry_in = max(0.0, self._reset_seconds - (self._clock() - self._opened_at))
            raise CircuitOpenError(
                f"{self.name} is temporarily unavailable after repeated failures.",
                details={"retry_in_seconds": round(retry_in, 1)},
            )
        if state == "half_open":
            self._trial_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        self._trial_in_flight = False
        if self._opened_at is not None or self._failures >= self._threshold:
            self._opened_at = self._clock()
            log.warning("circuit_opened", breaker=self.name, failures=self._failures)


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    should_retry: Callable[[Exception], bool],
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run *operation*, retrying errors that `should_retry` accepts.

    Backoff is exponential with full jitter. The last error is re-raised unchanged.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt == attempts or not should_retry(exc):
                raise
            delay = random.uniform(0, min(max_delay, base_delay * 2 ** (attempt - 1)))  # noqa: S311
            log.info("retrying", attempt=attempt, delay_s=round(delay, 2), error=type(exc).__name__)
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
