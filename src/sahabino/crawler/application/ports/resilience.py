from typing import Protocol

from sahabino.crawler.domain.errors import CrawlerError


class ErrorClassifierPort(Protocol):
    def classify(self, error: BaseException) -> CrawlerError: ...


class CircuitBreakerPort(Protocol):
    def before_call(self) -> int | None: ...

    def record_success(self, *, call_token: int | None = None) -> None: ...

    def record_failure(
        self,
        error: BaseException,
        *,
        proxied: bool = False,
        call_token: int | None = None,
    ) -> None: ...
