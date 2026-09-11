from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from sahabino.crawler.domain.errors import (
    AccessForbidden,
    AppGone,
    AppNotFound,
    ClientRequestFailure,
    CrawlerError,
    GatewayFailure,
    LegalRestriction,
    NetworkTimeout,
    ProxyAuthenticationFailure,
    RateLimited,
    UpstreamFailure,
)


def retry_after_seconds(
    value: str | None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            return None
        return max(0.0, (retry_at.astimezone(UTC) - now()).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def retry_after_header(headers: Mapping[str, object] | None) -> str | None:
    if headers is None:
        return None
    return next(
        (str(value) for key, value in headers.items() if str(key).lower() == "retry-after"),
        None,
    )


def classify_http_status(
    status_code: int,
    *,
    retry_after: float | None = None,
    proxied: bool = False,
) -> CrawlerError | None:
    if status_code < 400:
        return None
    if status_code == 403:
        return AccessForbidden(
            "Google Play returned HTTP 403",
            retry_with_new_egress=proxied,
        )
    if status_code == 407:
        return ProxyAuthenticationFailure(
            "proxy authentication was rejected",
            retry_with_new_egress=proxied,
        )
    if status_code == 408:
        return NetworkTimeout("Google Play returned HTTP 408")
    if status_code == 429:
        return RateLimited(
            "Google Play returned HTTP 429",
            retry_after_seconds=retry_after,
        )
    if status_code == 404:
        return AppNotFound("application is unavailable in Google Play")
    if status_code == 410:
        return AppGone("application is no longer available in Google Play")
    if status_code == 451:
        return LegalRestriction("Google Play returned HTTP 451")
    if status_code in {502, 504}:
        return GatewayFailure(f"Google Play returned HTTP {status_code}")
    if status_code >= 500:
        return UpstreamFailure(
            f"Google Play returned HTTP {status_code}",
            retry_after_seconds=retry_after,
        )
    return ClientRequestFailure(f"Google Play returned HTTP {status_code}")
