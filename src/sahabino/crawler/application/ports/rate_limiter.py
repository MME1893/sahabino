from typing import Protocol


class GlobalRateLimiter(Protocol):
    def acquire(self) -> None: ...
