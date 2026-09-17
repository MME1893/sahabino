from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from sahabino.crawler.domain.dto import (
    AdapterCapabilities,
    AppDetailsDTO,
    ReviewDTO,
    ReviewsDTO,
)
from sahabino.crawler.domain.errors import ParseFailure, SchemaFailure
from sahabino.crawler.infrastructure.adapters.common import (
    ReviewRecord,
    invalid_review_record,
    normalize_review_batch,
    normalize_updated_on,
    validate_package,
)

logger = logging.getLogger(__name__)

ADAPTER_NAME = "google-play-scraper"
_REVIEW_FIELDS = frozenset({"reviewId", "userName", "score", "content", "thumbsUpCount"})
_NORMALIZED_FIELD_NAMES = {
    "reviewId": "external_review_id",
    "userName": "author_name",
    "score": "score",
    "content": "content",
    "thumbsUpCount": "thumbs_up_count",
}

AppFetcher = Callable[..., dict[str, Any]]
ReviewFetcher = Callable[..., list[ReviewRecord]]


def _default_app_fetcher(**kwargs: Any) -> dict[str, Any]:
    from google_play_scraper import app

    return cast(dict[str, Any], app(**kwargs))


def _default_review_fetcher(**kwargs: Any) -> list[ReviewRecord]:
    from google_play_scraper import Sort
    from google_play_scraper.constants.element import ElementSpecs
    from google_play_scraper.constants.request import Formats
    from google_play_scraper.features.reviews import MAX_COUNT_EACH_FETCH, _fetch_review_items

    app_id = str(kwargs["app_id"])
    language_code = str(kwargs["lang"])
    country_code = str(kwargs["country"])
    count = int(kwargs["count"])
    url = Formats.Reviews.build(lang=language_code, country=country_code)
    result: list[ReviewRecord] = []
    token: Any = None
    seen_tokens: set[str] = set()
    while len(result) < count:
        fetch_count = min(count - len(result), MAX_COUNT_EACH_FETCH)
        items, next_token = _fetch_review_items(
            url,
            app_id,
            Sort.NEWEST.value,
            fetch_count,
            None,
            None,
            token,
        )
        if not isinstance(items, list):
            raise ParseFailure("secondary reviews parser returned an invalid page")
        for item in items[:fetch_count]:
            result.append(_extract_review_item(item, ElementSpecs.Review))
        if not items or len(result) >= count or next_token is None:
            break
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            raise ParseFailure("secondary reviews pagination token is invalid")
        seen_tokens.add(next_token)
        token = next_token
    return result


def _extract_review_item(item: Any, specs: dict[str, Any]) -> ReviewRecord:
    raw: dict[str, Any] = {}
    for key, spec in specs.items():
        if key not in _REVIEW_FIELDS:
            continue
        try:
            raw[key] = spec.extract_content(item)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            return invalid_review_record(_NORMALIZED_FIELD_NAMES[key], error)
    try:
        timestamp = item[5][0] if len(item) > 5 and item[5] else None
        raw["at"] = datetime.fromtimestamp(timestamp, UTC) if timestamp is not None else None
    except (IndexError, KeyError, TypeError, ValueError, OverflowError, OSError) as error:
        return invalid_review_record("source_at", error)
    return raw


class GooglePlayScraperAdapter:
    capabilities = AdapterCapabilities(
        supports_proxy=False,
        transport_controlled=False,
        supports_reviews=True,
    )
    network_mode = "DIRECT_ONLY"

    def __init__(
        self,
        *,
        app_fetcher: AppFetcher = _default_app_fetcher,
        review_fetcher: ReviewFetcher = _default_review_fetcher,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._app_fetcher = app_fetcher
        self._review_fetcher = review_fetcher
        self._now = now

    def get_app(self, package_name: str, language_code: str, country_code: str) -> AppDetailsDTO:
        validate_package(package_name)
        raw = self._app_fetcher(
            app_id=package_name,
            lang=language_code,
            country=country_code,
        )
        updated = raw.get("updated", raw.get("lastUpdatedOn"))
        return AppDetailsDTO.model_validate(
            {
                "min_installs": raw.get("minInstalls"),
                "score": raw.get("score"),
                "ratings_count": raw.get("ratings"),
                "reviews_count": raw.get("reviews"),
                "store_updated_on": normalize_updated_on(updated),
                "version": raw.get("version"),
                "ad_supported": bool(raw.get("adSupported")),
                "collected_at": self._now(),
                "source_adapter": "google-play-scraper",
            }
        )

    def get_reviews(
        self,
        package_name: str,
        language_code: str,
        country_code: str,
        limit: int,
    ) -> ReviewsDTO:
        validate_package(package_name)
        effective_limit = min(limit, 1000)
        if effective_limit < 1:
            raise ValueError("review limit must be positive")
        raw_reviews = self._review_fetcher(
            app_id=package_name,
            lang=language_code,
            country=country_code,
            count=effective_limit,
        )
        if not isinstance(raw_reviews, list):
            raise ParseFailure("secondary reviews parser returned an invalid response set")
        observed_at = self._now()
        return normalize_review_batch(
            raw_reviews,
            package_name=package_name,
            adapter_name=ADAPTER_NAME,
            requested_count=limit,
            effective_limit=effective_limit,
            observed_at=observed_at,
            normalize=self._normalize_review,
            logger=logger,
        )

    def close(self) -> None:
        return

    @staticmethod
    def _normalize_review(raw: dict[str, Any], position: int, observed_at: datetime) -> ReviewDTO:
        source_at = raw.get("at")
        if isinstance(source_at, (int, float)):
            source_at = datetime.fromtimestamp(source_at, UTC)
        elif isinstance(source_at, str):
            source_at = datetime.fromisoformat(source_at.replace("Z", "+00:00"))
        if not isinstance(source_at, datetime):
            raise SchemaFailure("secondary review timestamp is missing")
        if source_at.tzinfo is None or source_at.utcoffset() is None:
            raise SchemaFailure("secondary review timestamp is ambiguous")
        return ReviewDTO.model_validate(
            {
                "external_review_id": raw.get("reviewId"),
                "source_at": source_at.astimezone(UTC),
                "author_name": raw.get("userName"),
                "thumbs_up_count": raw.get("thumbsUpCount"),
                "score": raw.get("score"),
                "content": raw.get("content"),
                "position": position,
                "observed_at": observed_at,
                "source_adapter": ADAPTER_NAME,
            }
        )
