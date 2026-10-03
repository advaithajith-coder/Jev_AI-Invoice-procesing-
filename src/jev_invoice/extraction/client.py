"""Provider-neutral LLM client: daily budget, circuit breaker, retries, cost accounting."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from jev_invoice.config import Settings
from jev_invoice.core.errors import BudgetExceededError, ExtractionError
from jev_invoice.core.resilience import CircuitBreaker, retry_async
from jev_invoice.extraction.providers import LLMProvider, LLMResponse
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

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def spent_today_usd(self) -> Decimal:
        self._roll_day()
        return self._spent_today

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
            )
        except Exception as exc:
            self._breaker.record_failure()
            log.warning("llm_call_failed", provider=self._provider.name, error=type(exc).__name__)
            raise ExtractionError(
                f"The extraction model call failed ({type(exc).__name__}).",
                details={"provider": self._provider.name, "error_type": type(exc).__name__},
            ) from exc
        self._breaker.record_success()

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

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._spend_day:
            self._spend_day = today
            self._spent_today = Decimal(0)
