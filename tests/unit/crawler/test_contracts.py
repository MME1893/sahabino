from __future__ import annotations

from datetime import UTC, date, datetime
from json import JSONDecodeError
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from sahabino.common.config import Settings
from sahabino.crawler.application.policies.adapter import AdapterFallbackPolicy
from sahabino.crawler.application.policies.retry import RetryPolicy
from sahabino.crawler.domain.dto import AppDetailsDTO, ReviewDTO, ReviewsDTO
from sahabino.crawler.domain.errors import (
    AccessForbidden,
    AdapterFailure,
    AppGone,
    AppNotFound,
    ClientRequestFailure,
    CrawlerError,
    GatewayFailure,
    InvalidPackage,
    LegalRestriction,
    LocalRateLimitWaitExceeded,
    NetworkTimeout,
    ParseFailure,
    ProxyAuthenticationFailure,
    ProxyConnectionFailure,
    RateLimited,
    SchemaFailure,
    TemporaryConnectionFailure,
    UpstreamFailure,
    safe_error_message,
)
from sahabino.crawler.infrastructure.adapters.classifier import ErrorClassifier
from sahabino.crawler.infrastructure.adapters.common import normalize_updated_on
from sahabino.crawler.infrastructure.adapters.google_play import GooglePlayScraperAdapter
from sahabino.crawler.infrastructure.http_errors import (
    classify_http_status,
    retry_after_header,
    retry_after_seconds,
)
from sahabino.messaging.playstore_events import (
    APP_STATS_EVENT_TYPE,
    AppStatsCollectedV1,
    ReviewObservedV1,
    app_stats_envelope,
)

from .fakes import FakeClock

NOW = datetime(2026, 9, 6, 12, 30, tzinfo=UTC)


def _named_error(name: str, message: str = "library failure") -> Exception:
    return type(name, (RuntimeError,), {})(message)


def test_app_details_validate_counts_score_and_aware_timestamp() -> None:
    with pytest.raises(ValidationError):
        AppDetailsDTO(
            min_installs=-1,
            score=6,
            ratings_count=1,
            reviews_count=1,
            store_updated_on=None,
            version=None,
            ad_supported=False,
            collected_at=datetime(2026, 1, 1),
            source_adapter="primary",
        )


def test_review_contract_rejects_naive_source_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ReviewDTO(
            external_review_id="review-1",
            source_at=datetime(2026, 1, 1),
            author_name="Author",
            thumbs_up_count=0,
            score=5,
            content="Good",
            position=1,
            observed_at=NOW,
            source_adapter="primary",
        )


def _review(position: int) -> ReviewDTO:
    return ReviewDTO(
        external_review_id=f"review-{position}",
        source_at=NOW,
        author_name="Author",
        thumbs_up_count=0,
        score=5,
        content="Good",
        position=position,
        observed_at=NOW,
        source_adapter="primary",
    )


def test_review_position_1000_is_accepted_and_1001_is_rejected() -> None:
    assert _review(1000).position == 1000

    with pytest.raises(ValidationError):
        _review(1001)

    event = ReviewObservedV1(
        crawl_task_id=uuid4(),
        application_id=uuid4(),
        package_name="com.example.app",
        **_review(1000).model_dump(),
    )
    assert event.position == 1000

    with pytest.raises(ValidationError):
        ReviewObservedV1(
            crawl_task_id=uuid4(),
            application_id=uuid4(),
            package_name="com.example.app",
            **{
                **_review(1000).model_dump(),
                "position": 1001,
            },
        )


def test_reviews_collection_accepts_1000_and_rejects_1001_items() -> None:
    review = _review(1)

    assert len(ReviewsDTO(reviews=(review,) * 1000).reviews) == 1000
    with pytest.raises(ValidationError):
        ReviewsDTO(reviews=(review,) * 1001)


def test_app_event_uses_date_precision_and_expected_envelope() -> None:
    payload = AppStatsCollectedV1(
        crawl_task_id=uuid4(),
        application_id=uuid4(),
        package_name="com.example.app",
        collected_at=NOW,
        min_installs=10,
        score=4.5,
        ratings_count=8,
        reviews_count=4,
        store_updated_on=date(2026, 9, 5),
        version="1.0",
        ad_supported=False,
        source_adapter="gplay-scraper",
    )

    envelope = app_stats_envelope(payload)

    assert envelope.event_type == APP_STATS_EVENT_TYPE
    assert envelope.schema_version == 1
    assert envelope.payload.store_updated_on == date(2026, 9, 5)
    assert "store_updated_at" not in envelope.model_dump_json()


def test_secondary_capabilities_are_direct_only() -> None:
    adapter = GooglePlayScraperAdapter(app_fetcher=lambda **_: {}, review_fetcher=lambda **_: [])

    assert adapter.network_mode == "DIRECT_ONLY"
    assert adapter.capabilities.supports_proxy is False
    assert adapter.capabilities.transport_controlled is False
    assert adapter.capabilities.supports_reviews is True


@pytest.mark.parametrize("error", [ParseFailure("bad"), SchemaFailure("bad")])
def test_parser_and_schema_failures_are_secondary_eligible(error: Exception) -> None:
    assert AdapterFallbackPolicy().should_use_secondary(error) is True


@pytest.mark.parametrize("error", [RateLimited("slow"), AppNotFound("missing")])
def test_throttle_and_not_found_are_not_secondary_eligible(error: Exception) -> None:
    assert AdapterFallbackPolicy().should_use_secondary(error) is False


@pytest.mark.parametrize(
    "error",
    [
        AccessForbidden("403"),
        ProxyAuthenticationFailure("407"),
        LocalRateLimitWaitExceeded("local"),
        AppGone("410"),
        LegalRestriction("451"),
        ClientRequestFailure("400"),
    ],
)
def test_http_proxy_and_local_failures_never_use_secondary(error: Exception) -> None:
    assert AdapterFallbackPolicy().should_use_secondary(error) is False


def test_error_classifier_preserves_429_retry_after() -> None:
    raw = RuntimeError("HTTP 429")
    raw.retry_after_seconds = 7  # type: ignore[attr-defined]

    classified = ErrorClassifier().classify(raw)

    assert isinstance(classified, RateLimited)
    assert classified.retry_after_seconds == 7


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [(404, AppNotFound), (503, UpstreamFailure)],
)
def test_error_classifier_maps_http_status_attributes(
    status: int, expected_type: type[Exception]
) -> None:
    raw = RuntimeError("upstream request failed")
    raw.code = status  # type: ignore[attr-defined]

    assert isinstance(ErrorClassifier().classify(raw), expected_type)


def test_error_classifier_preserves_existing_domain_error_instance() -> None:
    original = RateLimited("classified", retry_after_seconds=4)

    assert ErrorClassifier().classify(original) is original


def test_error_classifier_maps_pydantic_validation_failure_to_schema_failure() -> None:
    with pytest.raises(ValidationError) as captured:
        AppDetailsDTO.model_validate({})

    assert isinstance(ErrorClassifier().classify(captured.value), SchemaFailure)


@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [
        (_named_error("InvalidAppIdError"), InvalidPackage),
        (_named_error("AppNotFoundError"), AppNotFound),
        (_named_error("NotFoundError"), AppNotFound),
        (_named_error("DataParsingError"), ParseFailure),
        (JSONDecodeError("malformed", "{", 1), ParseFailure),
        (_named_error("RateLimitError"), RateLimited),
        (RuntimeError("server returned 429"), RateLimited),
        (RuntimeError("proxy handshake failed"), ProxyConnectionFailure),
        (RuntimeError("operation timeout"), NetworkTimeout),
        (RuntimeError("connection reset"), TemporaryConnectionFailure),
        (RuntimeError("network unreachable"), TemporaryConnectionFailure),
        (IndexError("shape"), AdapterFailure),
        (KeyError("field"), AdapterFailure),
        (TypeError("unexpected value"), AdapterFailure),
    ],
)
def test_error_classifier_library_and_message_mapping_matrix(
    raw: Exception,
    expected_type: type[CrawlerError],
) -> None:
    assert isinstance(ErrorClassifier().classify(raw), expected_type)


def test_error_classifier_unknown_exception_uses_conservative_generic_error() -> None:
    class UnrecognizedLibraryFailure(Exception):
        pass

    classified = ErrorClassifier().classify(UnrecognizedLibraryFailure("opaque"))

    assert type(classified) is CrawlerError


@pytest.mark.parametrize("attribute", ["status_code", "code"])
def test_error_classifier_extracts_http_status_from_supported_attributes(attribute: str) -> None:
    raw = RuntimeError("request failed")
    setattr(raw, attribute, 429)

    classified = ErrorClassifier().classify(raw)

    assert isinstance(classified, RateLimited)


@pytest.mark.parametrize("invalid_status", ["not-numeric", object()])
def test_error_classifier_ignores_invalid_http_status_values(invalid_status: object) -> None:
    raw = RuntimeError("opaque failure")
    raw.status_code = invalid_status  # type: ignore[attr-defined]

    assert type(ErrorClassifier().classify(raw)) is CrawlerError


def test_error_classifier_preserves_retry_after_from_case_insensitive_headers() -> None:
    raw = RuntimeError("throttled")
    raw.status_code = 429  # type: ignore[attr-defined]
    raw.headers = {"rEtRy-AfTeR": "19"}  # type: ignore[attr-defined]

    classified = ErrorClassifier().classify(raw)

    assert isinstance(classified, RateLimited)
    assert classified.retry_after_seconds == 19


def test_retry_eligibility_is_explicit() -> None:
    policy = RetryPolicy(3, 30, clock=FakeClock())

    assert policy.is_retryable(RateLimited("slow")) is True
    assert policy.is_retryable(ParseFailure("bad")) is False


def test_settings_validate_proxy_relationships() -> None:
    with pytest.raises(ValidationError, match="direct fallback"):
        Settings(
            database_url="postgresql+psycopg://localhost/sahabino",
            playstore_proxy_enabled=True,
            playstore_proxy_urls=[],
            playstore_proxy_direct_fallback=False,
        )


def test_settings_allow_explicit_direct_only_proxy_mode() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        playstore_proxy_enabled=True,
        playstore_proxy_urls=[],
        playstore_proxy_direct_fallback=True,
    )

    assert settings.playstore_proxy_urls == []


def test_proxy_credentials_are_secret_at_settings_boundary() -> None:
    password = "settings-secret-password"
    settings = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        playstore_proxy_enabled=True,
        playstore_proxy_urls=[f"http://user:{password}@proxy.example:8080"],
    )

    assert isinstance(settings.playstore_proxy_urls[0], SecretStr)
    assert settings.playstore_proxy_urls[0].get_secret_value().endswith("@proxy.example:8080")
    assert password not in repr(settings)
    assert password not in str(settings)


def test_lifecycle_error_text_redacts_credential_bearing_urls() -> None:
    message = safe_error_message(
        RuntimeError("could not reach http://user:secret@proxy.example:8080/path")
    )

    assert "user" not in message
    assert "secret" not in message
    assert "proxy.example" not in message


@pytest.mark.parametrize("header_name", ["Retry-After", "retry-after", "RETRY-AFTER"])
def test_retry_after_header_lookup_is_case_insensitive(header_name: str) -> None:
    assert retry_after_header({header_name: "17"}) == "17"


def test_numeric_retry_after_is_parsed_directly() -> None:
    assert retry_after_seconds("7.5") == 7.5


def test_retry_after_http_date_parsing_uses_injected_time() -> None:
    now = datetime(2026, 9, 6, 12, tzinfo=UTC)

    assert (
        retry_after_seconds(
            "Sun, 06 Sep 2026 12:02:00 GMT",
            now=lambda: now,
        )
        == 120
    )
    assert (
        retry_after_seconds(
            "Sun, 06 Sep 2026 11:59:00 GMT",
            now=lambda: now,
        )
        == 0
    )
    assert retry_after_seconds("not-a-date", now=lambda: now) is None


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        (200, type(None)),
        (399, type(None)),
        (403, AccessForbidden),
        (404, AppNotFound),
        (407, ProxyAuthenticationFailure),
        (408, NetworkTimeout),
        (410, AppGone),
        (429, RateLimited),
        (451, LegalRestriction),
        (502, GatewayFailure),
        (504, GatewayFailure),
        (500, UpstreamFailure),
        (503, UpstreamFailure),
        (418, ClientRequestFailure),
    ],
)
def test_http_status_mapping_matrix(status: int, expected_type: type[object]) -> None:
    assert isinstance(classify_http_status(status), expected_type)


def test_http_403_egress_retry_flag_depends_on_proxy_use() -> None:
    direct = classify_http_status(403, proxied=False)
    proxied = classify_http_status(403, proxied=True)

    assert isinstance(direct, AccessForbidden)
    assert isinstance(proxied, AccessForbidden)
    assert direct.retry_with_new_egress is False
    assert proxied.retry_with_new_egress is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("Never updated", None),
        (datetime(2026, 9, 5, 23, tzinfo=UTC), date(2026, 9, 5)),
        (date(2026, 9, 5), date(2026, 9, 5)),
        (1788566400, date(2026, 9, 5)),
        (1788566400.0, date(2026, 9, 5)),
        ("Sep 05, 2026", date(2026, 9, 5)),
        ("2026-09-05", date(2026, 9, 5)),
        ("unknown upstream value", None),
        (object(), None),
    ],
)
def test_normalize_updated_on_table(value: object, expected: date | None) -> None:
    assert normalize_updated_on(value) == expected


def test_normalize_updated_on_rejects_naive_datetime() -> None:
    with pytest.raises(SchemaFailure, match="naive"):
        normalize_updated_on(datetime(2026, 9, 5))
