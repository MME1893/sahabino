from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from sahabino.crawler.application.ports.rate_limiter import GlobalRateLimiter
from sahabino.crawler.application.ports.transport import TransportResponse
from sahabino.crawler.domain.errors import (
    AdapterFailure,
    CrawlerError,
    NetworkTimeout,
    ProxyConnectionFailure,
    TemporaryConnectionFailure,
)
from sahabino.crawler.infrastructure.http_errors import (
    classify_http_status,
    retry_after_header,
    retry_after_seconds,
)
from sahabino.crawler.infrastructure.proxy.models import NetworkContext


class _Response(Protocol):
    status_code: int
    text: str
    headers: Mapping[str, str]


# for testing stuff
class _Session(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> _Response: ...

    def close(self) -> None: ...


def _default_session() -> _Session:
    from curl_cffi import requests

    return cast(_Session, requests.Session(impersonate="chrome"))


class CurlCffiTransport:
    def __init__(
        self,
        network_context: NetworkContext,
        rate_limiter: GlobalRateLimiter,
        *,
        timeout_seconds: float,
        session_factory: Callable[[], _Session] = _default_session,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        self._context = network_context
        self._rate_limiter = rate_limiter
        self._timeout = timeout_seconds
        self._session = session_factory()
        self._closed = False

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: str | bytes | None = None,
        params: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        if self._closed:
            raise AdapterFailure("transport is closed")
        self._rate_limiter.acquire()
        kwargs: dict[str, Any] = {
            "headers": dict(headers or {}),
            "timeout": self._timeout,
        }
        if data is not None:
            kwargs["data"] = data
        if params is not None:
            kwargs["params"] = dict(params)
        proxy_url = self._context.lease.secret_url()
        if proxy_url is not None:
            kwargs["proxy"] = proxy_url
        try:
            response = self._session.request(method.upper(), url, **kwargs)
        except CrawlerError:
            raise
        except Exception as error:
            raise self._translate(error) from error

        status_code = int(response.status_code)
        headers_map = {str(key): str(value) for key, value in response.headers.items()}
        response_error = classify_http_status(
            status_code,
            retry_after=retry_after_seconds(retry_after_header(headers_map)),
            proxied=not self._context.lease.is_direct,
        )
        if response_error is not None:
            raise response_error
        return TransportResponse(status_code, response.text, headers_map)

    def close(self) -> None:
        if self._closed:
            return
        self._session.close()
        self._closed = True

    def _translate(self, error: Exception) -> CrawlerError:
        value = f"{type(error).__name__} {error}".lower()
        if "timeout" in value:
            return NetworkTimeout("Google Play request timed out")
        if not self._context.lease.is_direct and (
            "proxy" in value or "connect" in value or "resolve" in value
        ):
            return ProxyConnectionFailure("proxy connection failed")
        if "connect" in value or "network" in value or "resolve" in value:
            return TemporaryConnectionFailure("Google Play connection failed")
        return TemporaryConnectionFailure("Google Play transport failed")
