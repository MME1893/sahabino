from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from sahabino.crawler.domain.dto import (
    AdapterCapabilities,
    AppDetailsDTO,
    ReviewDTO,
    ReviewsDTO,
)
from sahabino.crawler.domain.errors import SchemaFailure
from sahabino.crawler.infrastructure.adapters.common import (
    normalize_updated_on,
    validate_package,
)

AppFetcher = Callable[..., dict[str, Any]]
ReviewFetcher = Callable[..., list[dict[str, Any]]]


def _default_app_fetcher(**kwargs: Any) -> dict[str, Any]:
    from google_play_scraper import app

    return cast(dict[str, Any], app(**kwargs))


def _default_review_fetcher(**kwargs: Any) -> list[dict[str, Any]]:
    from google_play_scraper import Sort
    from google_play_scraper.constants.element import ElementSpecs
    from google_play_scraper.constants.request import Formats
    from google_play_scraper.features.reviews import MAX_COUNT_EACH_FETCH, _fetch_review_items

    app_id = str(kwargs["app_id"])
    language_code = str(kwargs["lang"])
    country_code = str(kwargs["country"])
    count = int(kwargs["count"])
    url = Formats.Reviews.build(lang=language_code, country=country_code)
    result: list[dict[str, Any]] = []
    token: Any = None
    while len(result) < count:
        fetch_count = min(count - len(result), MAX_COUNT_EACH_FETCH)
        items, token = _fetch_review_items(
            url,
            app_id,
            Sort.NEWEST.value,
            fetch_count,
            None,
            None,
            token,
        )
        for item in items[:fetch_count]:
            raw = {
                key: spec.extract_content(item)
                for key, spec in ElementSpecs.Review.items()
                if key not in {"at", "repliedAt"}
            }
            timestamp = item[5][0] if len(item) > 5 and item[5] else None
            raw["at"] = datetime.fromtimestamp(timestamp, UTC) if timestamp is not None else None
            result.append(raw)
        if not items or not isinstance(token, str):
            break
    return result


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
        observed_at = self._now()
        return ReviewsDTO(
            reviews=tuple(
                self._normalize_review(raw, position, observed_at)
                for position, raw in enumerate(raw_reviews[:effective_limit], start=1)
            )
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
                "source_adapter": "google-play-scraper",
            }
        )
