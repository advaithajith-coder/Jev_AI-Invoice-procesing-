import pytest

from jev_invoice.core.resilience import CircuitBreaker, CircuitOpenError, retry_async


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _state(breaker: CircuitBreaker) -> str:
    # A plain function call, so the type checker does not narrow `state` between asserts.
    return breaker.state


def test_breaker_opens_after_threshold_and_half_opens_after_reset() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("x", failure_threshold=2, reset_seconds=30, clock=clock)

    breaker.record_failure()
    assert _state(breaker) == "closed"
    breaker.record_failure()
    assert _state(breaker) == "open"
    with pytest.raises(CircuitOpenError) as excinfo:
        breaker.before_call()
    assert excinfo.value.details["retry_in_seconds"] == 30

    clock.now = 30
    assert _state(breaker) == "half_open"
    breaker.before_call()  # the single trial call is allowed
    with pytest.raises(CircuitOpenError):
        breaker.before_call()  # a second concurrent call is not


def test_half_open_trial_success_closes() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("x", failure_threshold=1, reset_seconds=10, clock=clock)
    breaker.record_failure()
    clock.now = 10
    breaker.before_call()

    breaker.record_success()

    assert breaker.state == "closed"
    assert breaker.consecutive_failures == 0


def test_half_open_trial_failure_reopens_for_full_period() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("x", failure_threshold=1, reset_seconds=10, clock=clock)
    breaker.record_failure()
    clock.now = 10
    breaker.before_call()

    breaker.record_failure()

    assert breaker.state == "open"
    clock.now = 19.9
    assert breaker.state == "open"


async def _no_sleep(_: float) -> None:
    return None


async def test_retry_until_success() -> None:
    attempts = []

    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("blip")
        return "ok"

    result = await retry_async(
        flaky, attempts=3, should_retry=lambda e: isinstance(e, ConnectionError), sleep=_no_sleep
    )

    assert result == "ok" and len(attempts) == 3


async def test_retry_gives_up_and_reraises_last_error() -> None:
    async def always_fails() -> None:
        raise ConnectionError("down")

    with pytest.raises(ConnectionError, match="down"):
        await retry_async(always_fails, attempts=2, should_retry=lambda e: True, sleep=_no_sleep)


async def test_non_retryable_error_raised_immediately() -> None:
    attempts = []

    async def bad_key() -> None:
        attempts.append(1)
        raise PermissionError("bad key")

    with pytest.raises(PermissionError):
        await retry_async(
            bad_key,
            attempts=5,
            should_retry=lambda e: not isinstance(e, PermissionError),
            sleep=_no_sleep,
        )
    assert len(attempts) == 1
