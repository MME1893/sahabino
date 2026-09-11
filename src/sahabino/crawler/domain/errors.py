from __future__ import annotations

import re


class CrawlerError(Exception):
    code = "CRAWLER_ERROR"

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RateLimited(CrawlerError):
    code = "RATE_LIMITED"


class AccessForbidden(CrawlerError):
    code = "ACCESS_FORBIDDEN"

    def __init__(self, message: str, *, retry_with_new_egress: bool = False) -> None:
        super().__init__(message)
        self.retry_with_new_egress = retry_with_new_egress


class NetworkTimeout(CrawlerError):
    code = "NETWORK_TIMEOUT"


class LocalRateLimitWaitExceeded(CrawlerError):
    code = "LOCAL_RATE_LIMIT_WAIT_EXCEEDED"


class TemporaryConnectionFailure(CrawlerError):
    code = "CONNECTION_FAILED"


class ProxyConnectionFailure(CrawlerError):
    code = "PROXY_CONNECTION_FAILED"


class ProxyAuthenticationFailure(CrawlerError):
    code = "PROXY_AUTHENTICATION_FAILED"

    def __init__(self, message: str, *, retry_with_new_egress: bool = False) -> None:
        super().__init__(message)
        self.retry_with_new_egress = retry_with_new_egress


class ProxyUnavailable(CrawlerError):
    code = "PROXY_UNAVAILABLE"


class UpstreamFailure(CrawlerError):
    code = "UPSTREAM_FAILURE"


class GatewayFailure(UpstreamFailure):
    code = "UPSTREAM_GATEWAY_FAILURE"


class AppNotFound(CrawlerError):
    code = "APP_NOT_FOUND"


class AppGone(CrawlerError):
    code = "APP_GONE"


class LegalRestriction(CrawlerError):
    code = "LEGAL_RESTRICTION"


class ClientRequestFailure(CrawlerError):
    code = "CLIENT_REQUEST_FAILED"


class InvalidPackage(CrawlerError):
    code = "INVALID_PACKAGE"


class ParseFailure(CrawlerError):
    code = "PARSE_FAILURE"


class SchemaFailure(CrawlerError):
    code = "SCHEMA_FAILURE"


class AdapterFailure(CrawlerError):
    code = "ADAPTER_FAILURE"


class CircuitOpen(CrawlerError):
    code = "CIRCUIT_OPEN"


class RegistryUnavailable(CrawlerError):
    code = "REGISTRY_UNAVAILABLE"


class MessagingPublishFailure(CrawlerError):
    code = "KAFKA_PUBLISH_FAILED"


def safe_error_message(error: BaseException, *, maximum_length: int = 500) -> str:
    """Produce concise lifecycle text without URLs, credentials, or response bodies."""
    message = re.sub(r"\b(?:https?|socks[45]?)://\S+", "[redacted endpoint]", str(error))
    message = " ".join(message.split())
    return (message or type(error).__name__)[:maximum_length]
