from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any, TypeVar, cast

from sahabino.crawler.application.ports.clock import Clock
from sahabino.crawler.domain.errors import (
    AccessForbidden,
    NetworkTimeout,
    ProxyAuthenticationFailure,
    ProxyConnectionFailure,
    RateLimited,
    TemporaryConnectionFailure,
    UpstreamFailure,
)

# using this approach because of tenacity behavior
ResultT = TypeVar("ResultT")


class RetryPolicy:
    def __init__(
        self,
        max_attempts: int,
        max_delay_seconds: float,
        *,
        clock: Clock,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("retry attempts must be at least one")
        if max_delay_seconds <= 0:
            raise ValueError("maximum retry delay must be positive")
        self._max_attempts = max_attempts
        self._max_delay = max_delay_seconds
        self._clock = clock
        self._random = random_value

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def is_retryable(self, error: BaseException) -> bool:
        if isinstance(error, (AccessForbidden, ProxyAuthenticationFailure)):
            return error.retry_with_new_egress
        return isinstance(
            error,
            (
                RateLimited,
                NetworkTimeout,
                TemporaryConnectionFailure,
                ProxyConnectionFailure,
                UpstreamFailure,
            ),
        )

    def delay_for(self, attempt_number: int, error: BaseException | None) -> float:
        backoff = float(min(2 ** max(0, attempt_number - 1) + self._random(), self._max_delay))
        if (
            isinstance(error, (RateLimited, UpstreamFailure))
            and error.retry_after_seconds is not None
        ):
            return float(max(backoff, error.retry_after_seconds))
        return backoff

    def execute(
        self,
        operation: Callable[[int], ResultT],
        *,
        before_retry: Callable[[BaseException], None] | None = None,
    ) -> ResultT:
        from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

        def wait(retry_state: RetryCallState) -> float:
            outcome = retry_state.outcome
            error = outcome.exception() if outcome is not None and outcome.failed else None
            return self.delay_for(retry_state.attempt_number, error)

        def notify(retry_state: RetryCallState) -> None:
            if before_retry is None or retry_state.outcome is None:
                return
            error = retry_state.outcome.exception()
            if error is not None:
                before_retry(error)

        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            retry=retry_if_exception(self.is_retryable),
            wait=wait,
            sleep=self._clock.sleep,
            before_sleep=notify,
            reraise=True,
        )
        result: Any = None
        for attempt in retrying:
            with attempt:
                result = operation(attempt.retry_state.attempt_number)
        return cast(ResultT, result)
