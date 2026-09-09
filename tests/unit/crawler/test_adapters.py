from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

from sahabino.crawler.application.ports.transport import TransportResponse
from sahabino.crawler.domain.errors import RateLimited, SchemaFailure
from sahabino.crawler.infrastructure.adapters import google_play as google_play_module
from sahabino.crawler.infrastructure.adapters.google_play import GooglePlayScraperAdapter
from sahabino.crawler.infrastructure.adapters.gplay import GPlayScraperAdapter
from sahabino.crawler.infrastructure.transport.controlled_gplay import ControlledGPlayHttpClient

NOW = datetime(2026, 9, 6, 10, 30, tzinfo=UTC)
UPDATE_TIMESTAMP = 1788566400
REVIEW_TIMESTAMP = 1788602400


def test_secondary_adapter_has_no_sibling_primary_implementation_dependency() -> None:
    assert "infrastructure.adapters.gplay" not in inspect.getsource(google_play_module)


class FakeSpec:
    def __init__(self, index: int) -> None:
        self.index = index

    def extract_content(self, item: list[Any]) -> Any:
        return item[self.index]


class FakeAppScraper:
    def scrape_play_store_data(
        self, package_name: str, language_code: str, country_code: str
    ) -> dict[str, object]:
        return {"package": package_name, "locale": (language_code, country_code)}


class FakeAppParser:
    updated: object = UPDATE_TIMESTAMP

    def parse_app_data(self, *_: object) -> dict[str, object]:
        return {
            "minInstalls": 100,
            "score": 4.5,
            "ratings": 90,
            "reviews": 40,
            "updated": self.updated,
            "version": "1.2.3",
            "adSupported": True,
        }


def _review_item() -> list[Any]:
    return [
        "review-1",
        "Ada",
        5,
        None,
        "Excellent",
        [REVIEW_TIMESTAMP],
        7,
    ]


def _review_response() -> str:
    inner = json.dumps([[_review_item()]])
    outer = json.dumps([[None, None, inner]])
    return ")]}'\n\n" + outer


class FakeReviewsScraper:
    def scrape_reviews_data(self, *_: object) -> dict[str, list[str]]:
        return {"reviews": [_review_response()]}


class FakeControlledClient:
    def close(self) -> None:
        return


def _primary_adapter(updated: object = UPDATE_TIMESTAMP) -> GPlayScraperAdapter:
    adapter = object.__new__(GPlayScraperAdapter)
    adapter._http_client = FakeControlledClient()  # type: ignore[attr-defined]
    adapter._now = lambda: NOW  # type: ignore[attr-defined]
    adapter._app_scraper_type = FakeAppScraper  # type: ignore[attr-defined]
    adapter._app_parser_type = FakeAppParser  # type: ignore[attr-defined]
    adapter._reviews_scraper_type = FakeReviewsScraper  # type: ignore[attr-defined]
    adapter._app_scraper = FakeAppScraper()  # type: ignore[attr-defined]
    adapter._reviews_scraper = FakeReviewsScraper()  # type: ignore[attr-defined]
    adapter._review_specs = {  # type: ignore[attr-defined]
        "reviewId": FakeSpec(0),
        "userName": FakeSpec(1),
        "score": FakeSpec(2),
        "content": FakeSpec(4),
        "thumbsUpCount": FakeSpec(6),
    }
    FakeAppParser.updated = updated
    return adapter


def _secondary_adapter(updated: object = UPDATE_TIMESTAMP) -> GooglePlayScraperAdapter:
    return GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {
            "minInstalls": 100,
            "score": 4.5,
            "ratings": 90,
            "reviews": 40,
            "updated": updated,
            "version": "1.2.3",
            "adSupported": True,
        },
        review_fetcher=lambda **_: [
            {
                "reviewId": "review-1",
                "userName": "Ada",
                "score": 5,
                "content": "Excellent",
                "at": datetime.fromtimestamp(REVIEW_TIMESTAMP, UTC),
                "thumbsUpCount": 7,
            }
        ],
        now=lambda: NOW,
    )


def test_primary_and_secondary_app_normalization_have_semantic_parity() -> None:
    primary = _primary_adapter().get_app("com.example.app", "en", "us")
    secondary = _secondary_adapter().get_app("com.example.app", "en", "us")

    assert primary.model_dump(exclude={"source_adapter"}) == secondary.model_dump(
        exclude={"source_adapter"}
    )
    assert primary.store_updated_on == datetime.fromtimestamp(UPDATE_TIMESTAMP, UTC).date()


def test_primary_and_secondary_review_normalization_have_semantic_parity() -> None:
    primary = _primary_adapter().get_reviews("com.example.app", "en", "us", 100)
    secondary = _secondary_adapter().get_reviews("com.example.app", "en", "us", 100)

    assert primary.reviews[0].model_dump(exclude={"source_adapter"}) == secondary.reviews[
        0
    ].model_dump(exclude={"source_adapter"})


def test_both_adapters_reduce_source_updates_to_calendar_dates() -> None:
    primary = _primary_adapter("Sep 05, 2026").get_app("com.example.app", "en", "us")
    secondary = _secondary_adapter(UPDATE_TIMESTAMP).get_app("com.example.app", "en", "us")

    assert primary.store_updated_on == date(2026, 9, 5)
    assert secondary.store_updated_on == datetime.fromtimestamp(UPDATE_TIMESTAMP, UTC).date()


def test_secondary_rejects_naive_review_timestamp_instead_of_using_local_timezone() -> None:
    adapter = GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {},
        review_fetcher=lambda **_: [
            {
                "reviewId": "r",
                "userName": "Ada",
                "score": 5,
                "content": "ok",
                "at": datetime(2026, 1, 1),
                "thumbsUpCount": 0,
            }
        ],
    )

    with pytest.raises(SchemaFailure, match="ambiguous"):
        adapter.get_reviews("com.example.app", "en", "us", 100)


def test_secondary_stops_at_100_and_does_not_expose_continuation() -> None:
    reviews = [
        {
            "reviewId": f"r-{index}",
            "userName": "Ada",
            "score": 5,
            "content": "ok",
            "at": NOW,
            "thumbsUpCount": 0,
        }
        for index in range(120)
    ]
    adapter = GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {}, review_fetcher=lambda **_: reviews, now=lambda: NOW
    )

    normalized = adapter.get_reviews("com.example.app", "en", "us", 120)

    assert len(normalized.reviews) == 100
    assert normalized.reviews[-1].position == 100


class FakeTransport:
    def __init__(self, responses: list[TransportResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> TransportResponse:
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def _controlled(transport: FakeTransport) -> ControlledGPlayHttpClient:
    return ControlledGPlayHttpClient(
        transport,
        base_url="https://play.google.test",
        app_details_endpoint="/store/apps/details",
        batch_execute_endpoint="/_/PlayStoreUi/data/batchexecute",
        headers={"user-agent": "test"},
    )


def test_controlled_client_preserves_app_and_review_requests() -> None:
    transport = FakeTransport(
        [TransportResponse(200, "app", {}), TransportResponse(200, "reviews", {})]
    )
    client = _controlled(transport)

    assert client.fetch_app_page("com.example.app", "en", "us") == "app"
    assert client.fetch_reviews_batch("com.example.app", "en", "us", 2, 100) == "reviews"

    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][2]["params"] == {
        "id": "com.example.app",
        "hl": "en",
        "gl": "us",
    }
    assert transport.calls[1][0] == "POST"
    assert "f.req=" in transport.calls[1][2]["data"]


def test_controlled_client_does_not_hide_failure_with_backend_fallback() -> None:
    transport = FakeTransport([RateLimited("slow", retry_after_seconds=7)])
    client = _controlled(transport)

    with pytest.raises(RateLimited):
        client.fetch_app_page("com.example.app", "en", "us")

    assert len(transport.calls) == 1


def test_pinned_real_gplay_components_use_only_controlled_transport() -> None:
    pytest.importorskip("gplay_scraper")
    app_html = "AF_initDataCallback({key: 'ds:5', data: []});"
    transport = FakeTransport(
        [
            TransportResponse(200, app_html, {}),
            TransportResponse(200, _review_response(), {}),
        ]
    )
    adapter = GPlayScraperAdapter(_controlled(transport), now=lambda: NOW)
    adapter._app_parser_type = FakeAppParser  # type: ignore[attr-defined]

    adapter.get_app("com.example.app", "en", "us")
    adapter.get_reviews("com.example.app", "en", "us", 100)

    assert [call[0] for call in transport.calls] == ["GET", "POST"]
