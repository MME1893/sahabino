from __future__ import annotations

from pydantic import ValidationError

from sahabino.crawler.domain.errors import (
    AdapterFailure,
    AppNotFound,
    CrawlerError,
    InvalidPackage,
    NetworkTimeout,
    ParseFailure,
    ProxyConnectionFailure,
    RateLimited,
    SchemaFailure,
    TemporaryConnectionFailure,
)
from sahabino.crawler.infrastructure.http_errors import (
    classify_http_status,
    retry_after_header,
    retry_after_seconds,
)


class ErrorClassifier:
    """Translate library-specific exceptions before they reach application policy."""

    _KNOWN_ADAPTER_FAILURE_NAMES = {"IndexError", "KeyError", "TypeError"}

    def classify(self, error: BaseException) -> CrawlerError:
        if isinstance(error, CrawlerError):
            return error
        if isinstance(error, ValidationError):
            return SchemaFailure("scraper response did not satisfy the normalized schema")

        name = type(error).__name__
        lowered = f"{name} {error}".lower()
        raw_status = getattr(error, "status_code", getattr(error, "code", None))
        try:
            status = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status = None
        if status is not None:
            retry_after = getattr(error, "retry_after_seconds", None)
            if retry_after is None:
                retry_after = retry_after_seconds(
                    retry_after_header(getattr(error, "headers", None))
                )
            classified = classify_http_status(status, retry_after=retry_after)
            if classified is not None:
                return classified
        if name in {"InvalidAppIdError"}:
            return InvalidPackage("package name is invalid")
        if name in {"AppNotFoundError", "NotFoundError"}:
            return AppNotFound("application is unavailable in Google Play")
        if name in {"DataParsingError", "JSONDecodeError"}:
            return ParseFailure("Google Play response could not be parsed")
        if name in {"RateLimitError"} or "429" in lowered:
            retry_after = getattr(error, "retry_after_seconds", None)
            return RateLimited(
                "Google Play rate limited the request",
                retry_after_seconds=retry_after,
            )
        if "proxy" in lowered:
            return ProxyConnectionFailure("proxy connection failed")
        if "timeout" in lowered:
            return NetworkTimeout("Google Play request timed out")
        if "connection" in lowered or "network" in lowered:
            return TemporaryConnectionFailure("Google Play connection failed")
        if name in self._KNOWN_ADAPTER_FAILURE_NAMES:
            return AdapterFailure("known scraper implementation failure")
        return CrawlerError("unexpected Play Store adapter failure")
