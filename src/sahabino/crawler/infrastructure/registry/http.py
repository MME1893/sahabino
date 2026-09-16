from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, cast
from uuid import UUID

from sahabino.crawler.application.ports.clock import Clock
from sahabino.crawler.domain.dto import ApplicationRef
from sahabino.crawler.domain.errors import RegistryUnavailable


class _Response(Protocol):
    status_code: int

    def json(self) -> Any: ...


class _HttpClient(Protocol):
    def get(self, url: str, **kwargs: Any) -> _Response: ...

    def close(self) -> None: ...


def _httpx_client(base_url: str, timeout: float) -> _HttpClient:
    import httpx

    return cast(_HttpClient, httpx.Client(base_url=base_url, timeout=timeout))


class HttpApplicationRegistry:
    def __init__(
        self,
        base_url: str,
        *,
        clock: Clock,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        client_factory: Callable[[str, float], _HttpClient] = _httpx_client,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("registry retry attempts must be at least one")
        self._clock = clock
        self._attempts = max_attempts
        self._client = client_factory(base_url.rstrip("/"), timeout_seconds)

    def list_active_applications(self) -> list[ApplicationRef]:
        last_error: BaseException | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._client.get("/applications", params={"active": "true"})
                if response.status_code >= 500:
                    raise RegistryUnavailable(f"Registry returned HTTP {response.status_code}")
                if response.status_code != 200:
                    raise RegistryUnavailable(
                        "Registry rejected active application query with HTTP "
                        f"{response.status_code}"
                    )
                body = response.json()
                if not isinstance(body, list):
                    raise RegistryUnavailable("Registry returned a non-list response")
                return [self._application_ref(item) for item in body]
            except RegistryUnavailable as error:
                last_error = error
            except Exception as error:
                last_error = error
            if attempt < self._attempts:
                self._clock.sleep(min(2 ** (attempt - 1), 4))
        raise RegistryUnavailable("active applications could not be fetched") from last_error

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _application_ref(item: object) -> ApplicationRef:
        if not isinstance(item, dict):
            raise RegistryUnavailable("Registry application item is not an object")
        try:
            return ApplicationRef(
                application_id=UUID(str(item["id"])),
                name=str(item["name"]),
                package_name=str(item["package_name"]),
                language_code=(
                    str(item["language_code"]) if item.get("language_code") is not None else None
                ),
                country_code=(
                    str(item["country_code"]) if item.get("country_code") is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RegistryUnavailable("Registry application item is invalid") from error
