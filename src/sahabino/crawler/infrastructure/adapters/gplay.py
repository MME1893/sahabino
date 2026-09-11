from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any

from sahabino.crawler.domain.dto import (
    AdapterCapabilities,
    AppDetailsDTO,
    ReviewDTO,
    ReviewsDTO,
)
from sahabino.crawler.domain.errors import InvalidPackage, ParseFailure, SchemaFailure
from sahabino.crawler.infrastructure.adapters.common import (
    normalize_updated_on,
    validate_package,
)
from sahabino.crawler.infrastructure.transport.controlled_gplay import ControlledGPlayHttpClient

GPLAY_VERSION = "1.0.6"


class GPlayScraperAdapter:
    capabilities = AdapterCapabilities(
        supports_proxy=True,
        transport_controlled=True,
        supports_reviews=True,
    )

    def __init__(
        self,
        http_client: ControlledGPlayHttpClient,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if version("gplay-scraper") != GPLAY_VERSION:
            raise RuntimeError(f"controlled integration requires gplay-scraper=={GPLAY_VERSION}")
        from gplay_scraper.core.gplay_parser import AppParser
        from gplay_scraper.core.gplay_scraper import AppScraper, ReviewsScraper
        from gplay_scraper.models.element_specs import ElementSpecs

        self._http_client = http_client
        self._now = now
        self._app_scraper_type = AppScraper
        self._app_parser_type = AppParser
        self._reviews_scraper_type = ReviewsScraper
        self._review_specs = ElementSpecs.Review
        self._app_scraper = object.__new__(AppScraper)
        self._app_scraper.http_client = http_client
        self._reviews_scraper = object.__new__(ReviewsScraper)
        self._reviews_scraper.http_client = http_client

    def get_app(self, package_name: str, language_code: str, country_code: str) -> AppDetailsDTO:
        validate_package(package_name)
        try:
            scrape = inspect.unwrap(self._app_scraper_type.scrape_play_store_data)
            dataset = scrape(self._app_scraper, package_name, language_code, country_code)
            parser = self._app_parser_type()
            parse = inspect.unwrap(self._app_parser_type.parse_app_data)
            details = parse(parser, dataset, package_name, None, None)
            if not isinstance(details, dict):
                raise ParseFailure("gplay app parser returned no details")
            return AppDetailsDTO.model_validate(
                {
                    "min_installs": details.get("minInstalls"),
                    "score": details.get("score"),
                    "ratings_count": details.get("ratings"),
                    "reviews_count": details.get("reviews"),
                    "store_updated_on": normalize_updated_on(details.get("updated")),
                    "version": details.get("version"),
                    "ad_supported": bool(details.get("adSupported")),
                    "collected_at": self._now(),
                    "source_adapter": "gplay-scraper",
                }
            )
        except (InvalidPackage, ParseFailure, SchemaFailure):
            raise

    def get_reviews(
        self,
        package_name: str,
        language_code: str,
        country_code: str,
        limit: int,
    ) -> ReviewsDTO:
        validate_package(package_name)
        effective_limit = min(limit, 100)
        if effective_limit < 1:
            raise ValueError("review limit must be positive")
        scrape = inspect.unwrap(self._reviews_scraper_type.scrape_reviews_data)
        dataset = scrape(
            self._reviews_scraper,
            package_name,
            effective_limit,
            language_code,
            country_code,
            2,
        )
        responses = dataset.get("reviews") if isinstance(dataset, dict) else None
        if not isinstance(responses, list):
            raise ParseFailure("gplay reviews parser returned an invalid response set")
        parsed: list[dict[str, Any]] = []
        for response in responses:
            for item in decode_gplay_review_response(response):
                raw = {
                    key: spec.extract_content(item)
                    for key, spec in self._review_specs.items()
                    if key != "at"
                }
                timestamp = item[5][0] if len(item) > 5 and item[5] else None
                raw["at"] = (
                    datetime.fromtimestamp(timestamp, UTC) if timestamp is not None else None
                )
                parsed.append(raw)
        observed_at = self._now()
        normalized = tuple(
            self._normalize_review(raw, index, observed_at)
            for index, raw in enumerate(parsed[:effective_limit], start=1)
        )
        return ReviewsDTO(reviews=normalized)

    def close(self) -> None:
        self._http_client.close()

    def _normalize_review(
        self, raw: dict[str, Any], position: int, observed_at: datetime
    ) -> ReviewDTO:
        value = raw.get("at")
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise SchemaFailure("gplay review timestamp is not timezone-aware")
        return ReviewDTO.model_validate(
            {
                "external_review_id": raw.get("reviewId"),
                "source_at": value,
                "author_name": raw.get("userName"),
                "thumbs_up_count": raw.get("thumbsUpCount"),
                "score": raw.get("score"),
                "content": raw.get("content"),
                "position": position,
                "observed_at": observed_at,
                "source_adapter": "gplay-scraper",
            }
        )


def decode_gplay_review_response(response: str) -> list[list[Any]]:
    """Compatibility helper used by the UTC parser guard tests."""
    match = re.search(r"\)\]\}'\n\n([\s\S]+)", response)
    if match is None:
        raise ParseFailure("gplay review response marker was not found")
    outer = json.loads(match.group(1))
    data = json.loads(outer[0][2])
    return data[0] if data and data[0] else []
