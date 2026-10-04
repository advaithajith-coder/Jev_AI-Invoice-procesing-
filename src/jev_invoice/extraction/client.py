"""Provider-neutral LLM client: daily budget, circuit breaker, retries, cost accounting."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from jev_invoice.config import Settings
from jev_invoice.core.errors import BudgetExceededError, ExtractionError
from jev_invoice.core.resilience import CircuitBreaker, retry_async
from jev_invoice.extraction.providers import LLMProvider, LLMResponse, RateLimit
from jev_invoice.ingest.parsing import PageImage

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LLMCall:
    """One completed LLM call with its cost and latency."""

    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: float


@dataclass
class _Allowance:
    """A per-minute token allowance that refills steadily, estimated between calls."""

    limit: int
    tokens: float
    refill_per_second: float
    at: float

    @classmethod
    def from_rate_limit(cls, rate: RateLimit, now: float) -> "_Allowance":
        used = rate.limit_tokens - rate.remaining_tokens
        if used > 0 and rate.reset_seconds > 0:
            refill = used / rate.reset_seconds
        else:
            refill = rate.limit_tokens / 60  # full: assume a per-minute allowance
        return cls(rate.limit_tokens, float(rate.remaining_tokens), refill, now)

    def available(self, now: float) -> float:
        return min(self.limit, self.tokens + self.refill_per_second * (now - self.at))


class LLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        settings: Settings,
        *,
        breaker: CircuitBreaker | None = None,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
        cost_per_input_token: Decimal | None = None,
        cost_per_output_token: Decimal | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._cost_in = (
            settings.llm_cost_per_input_token_usd
            if cost_per_input_token is None
            else cost_per_input_token
        )
        self._cost_out = (
            settings.llm_cost_per_output_token_usd
            if cost_per_output_token is None
            else cost_per_output_token
        )
        self._breaker = breaker or CircuitBreaker(
            f"LLM ({provider.name})",
            failure_threshold=settings.circuit_breaker_threshold,
            reset_seconds=settings.circuit_breaker_reset_seconds,
        )
        self._today = today
        self._spend_day = today()
        self._spent_today = Decimal(0)
        self._clock = clock
        self._sleep = sleep
        self._allowance: _Allowance | None = None
        self._gate = asyncio.Lock()

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def spent_today_usd(self) -> Decimal:
        self._roll_day()
        return self._spent_today

    async def wait_for_capacity(self) -> float:
        """Wait until the provider's per-minute token allowance has room for one call.

        The pipeline calls this before a timed step starts, so waiting on a rate limit
        is not counted as processing time. Callers queue here one at a time, and each
        reserves its share of the allowance. Returns the seconds waited: 0 when there is
        room, or when the provider reports no allowance (Ollama, tests).
        """
        async with self._gate:
            allowance = self._allowance
            if allowance is None or allowance.refill_per_second <= 0:
                return 0.0
            need = self._settings.llm_tokens_per_call
            available = allowance.available(self._clock())
            wait = 0.0
            if available < need:
                wait = min(
                    (need - available) / allowance.refill_per_second,
                    self._settings.llm_retry_after_max_seconds,
                )
                log.info("waiting_for_rate_limit", provider=self._provider.name, seconds=wait)
                await self._sleep(wait)
            now = self._clock()
            allowance.tokens = allowance.available(now) - need
            allowance.at = now
            return wait

    async def complete(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = False,
        images: Sequence[PageImage] = (),
    ) -> LLMCall:
        """Run one completion with budget, breaker, and retry protection.

        Raises:
            BudgetExceededError: today's spend limit is reached.
            CircuitOpenError: the provider failed repeatedly; calls are paused.
            ExtractionError: the call failed permanently or after all retries.
        """
        self._roll_day()
        if self._spent_today >= self._settings.llm_daily_budget_usd:
            raise BudgetExceededError(
                "Daily LLM budget reached; extraction paused until tomorrow (UTC).",
                details={"limit_usd": str(self._settings.llm_daily_budget_usd)},
            )
        self._breaker.before_call()

        start = time.monotonic()
        try:
            response = await retry_async(
                lambda: self._call(system, user, json_mode, images),
                attempts=self._settings.llm_max_attempts,
                should_retry=self._is_retryable,
                delay_hint=self._retry_after,
            )
        except Exception as exc:
            self._breaker.record_failure()
            log.warning("llm_call_failed", provider=self._provider.name, error=type(exc).__name__)
            raise ExtractionError(
                f"The extraction model call failed ({type(exc).__name__}).",
                details={"provider": self._provider.name, "error_type": type(exc).__name__},
            ) from exc
        self._breaker.record_success()
        if response.rate_limit is not None:
            self._allowance = _Allowance.from_rate_limit(response.rate_limit, self._clock())

        cost = response.input_tokens * self._cost_in + response.output_tokens * self._cost_out
        self._spent_today += cost
        call = LLMCall(
            text=response.text,
            provider=self._provider.name,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=cost,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
        )
        log.info(
            "llm_call",
            provider=call.provider,
            model=call.model,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cost_usd=str(call.cost_usd.quantize(Decimal("0.000001"))),
            latency_ms=call.latency_ms,
        )
        return call

    async def _call(
        self, system: str, user: str, json_mode: bool, images: Sequence[PageImage]
    ) -> LLMResponse:
        return await self._provider.complete(
            system=system,
            user=user,
            max_tokens=self._settings.llm_max_output_tokens,
            json_mode=json_mode,
            images=images,
        )

    def _is_retryable(self, exc: Exception) -> bool:
        # Permanent errors (bad key, unknown model) subclass the retryable base
        # (e.g. groq.APIError), so they are excluded explicitly.
        if isinstance(exc, self._provider.permanent_errors):
            return False
        return isinstance(exc, self._provider.retryable_errors)

    def _retry_after(self, exc: Exception) -> float | None:
        """The wait a rate-limited provider asked for (Retry-After, in seconds), capped."""
        headers = getattr(getattr(exc, "response", None), "headers", None)
        try:
            seconds = float(headers["retry-after"])  # type: ignore[index]
        except (TypeError, KeyError, ValueError):  # no header, or an HTTP date
            return None
        return min(max(seconds, 0.0), self._settings.llm_retry_after_max_seconds)

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._spend_day:
            self._spend_day = today
            self._spent_today = Decimal(0)
