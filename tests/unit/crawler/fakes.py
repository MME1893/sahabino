from __future__ import annotations

from threading import Lock


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.sleeps: list[float] = []
        self._lock = Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.value

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.value += seconds

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds
