from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    text: str
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: str | bytes | None = None,
        params: Mapping[str, str] | None = None,
    ) -> TransportResponse: ...

    def close(self) -> None: ...
