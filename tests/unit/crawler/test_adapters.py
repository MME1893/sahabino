from __future__ import annotations

import inspect
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import parse_qs

import pytest

from sahabino.crawler.application.ports.transport import TransportResponse
from sahabino.crawler.domain.errors import ParseFailure, RateLimited, SchemaFailure
from sahabino.crawler.infrastructure.adapters import google_play as google_play_module
from sahabino.crawler.infrastructure.adapters import gplay as gplay_module
from sahabino.crawler.infrastructure.adapters.google_play import GooglePlayScraperAdapter
from sahabino.crawler.infrastructure.adapters.gplay import (
    GPlayScraperAdapter,
    decode_gplay_review_response,
)
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


def _review_item(
    review_id: object = "review-1",
    author: object = "Ada",
    score: object = 5,
    content: object = "Excellent",
    timestamp: object = REVIEW_TIMESTAMP,
    thumbs_up_count: object = 7,
) -> list[Any]:
    return [
        review_id,
        author,
        score,
        None,
        content,
        [timestamp] if timestamp is not None else None,
        thumbs_up_count,
    ]


def _review_response(items: list[list[Any]] | None = None) -> str:
    inner = json.dumps([[*items]] if items is not None else [[_review_item()]])
    outer = json.dumps([[None, None, inner]])
    return ")]}'\n\n" + outer


def _raw_review(index: int, **overrides: object) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "reviewId": f"review-{index}",
        "userName": "Ada",
        "score": 5,
        "content": "Excellent",
        "at": NOW,
        "thumbsUpCount": 7,
    }
    raw.update(overrides)
    return raw


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


def _primary_review_adapter(pages: list[list[list[Any]]]) -> GPlayScraperAdapter:
    class PageReviewsScraper:
        def scrape_reviews_data(self, *_: object) -> dict[str, list[str]]:
            return {"reviews": [_review_response(items) for items in pages]}

    adapter = _primary_adapter()
    adapter._reviews_scraper_type = PageReviewsScraper  # type: ignore[attr-defined]
    adapter._reviews_scraper = PageReviewsScraper()  # type: ignore[attr-defined]
    return adapter


def _secondary_review_adapter(records: list[dict[str, Any]]) -> GooglePlayScraperAdapter:
    return GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {},
        review_fetcher=lambda **_: records,
        now=lambda: NOW,
    )


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


def test_primary_app_parser_non_mapping_is_parse_failure() -> None:
    class NonMappingParser:
        def parse_app_data(self, *_: object) -> object:
            return None

    adapter = _primary_adapter()
    adapter._app_parser_type = NonMappingParser  # type: ignore[attr-defined]

    with pytest.raises(ParseFailure, match="no details"):
        adapter.get_app("com.example.app", "en", "us")


@pytest.mark.parametrize("dataset", [None, {}, {"reviews": None}, {"reviews": "invalid"}])
def test_primary_rejects_invalid_reviews_dataset(dataset: object) -> None:
    class InvalidReviewsScraper:
        def scrape_reviews_data(self, *_: object) -> object:
            return dataset

    adapter = _primary_adapter()
    adapter._reviews_scraper_type = InvalidReviewsScraper  # type: ignore[attr-defined]
    adapter._reviews_scraper = InvalidReviewsScraper()  # type: ignore[attr-defined]

    with pytest.raises(ParseFailure, match="invalid response set"):
        adapter.get_reviews("com.example.app", "en", "us", 10)


def test_primary_review_decoder_requires_response_marker() -> None:
    with pytest.raises(ParseFailure, match="marker"):
        decode_gplay_review_response("unmarked response")


@pytest.mark.parametrize(
    "response",
    [
        ")]}'\n\n{",
        ")]}'\n\n" + json.dumps([[None, None, "{"]]),
    ],
)
def test_primary_review_decoder_exposes_raw_json_errors_for_classifier(response: str) -> None:
    with pytest.raises(json.JSONDecodeError):
        decode_gplay_review_response(response)


@pytest.mark.parametrize("timestamp", [None, datetime(2026, 9, 6, 12)])
def test_primary_review_normalization_rejects_missing_or_naive_timestamp(
    timestamp: object,
) -> None:
    adapter = _primary_adapter()
    raw = {
        "reviewId": "review-1",
        "userName": "Ada",
        "score": 5,
        "content": "ok",
        "at": timestamp,
        "thumbsUpCount": 0,
    }

    with pytest.raises(SchemaFailure, match="timezone-aware"):
        adapter._normalize_review(raw, 1, NOW)  # type: ignore[attr-defined]


@pytest.mark.parametrize("limit", [0, -1])
def test_primary_review_limit_must_be_positive(limit: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        _primary_adapter().get_reviews("com.example.app", "en", "us", limit)


def test_primary_accepts_1000_reviews_and_caps_larger_requests() -> None:
    captured_counts: list[int] = []

    class CapturingReviewsScraper:
        def scrape_reviews_data(
            self,
            _package: str,
            count: int,
            *_: object,
        ) -> dict[str, list[str]]:
            captured_counts.append(count)
            return {"reviews": []}

    adapter = _primary_adapter()
    adapter._reviews_scraper_type = CapturingReviewsScraper  # type: ignore[attr-defined]
    adapter._reviews_scraper = CapturingReviewsScraper()  # type: ignore[attr-defined]

    result = adapter.get_reviews("com.example.app", "en", "us", 1000)
    capped_result = adapter.get_reviews("com.example.app", "en", "us", 1001)

    assert captured_counts == [1000, 1000]
    assert result.reviews == ()
    assert capped_result.reviews == ()


def test_primary_empty_reviews_are_a_successful_empty_dto() -> None:
    class EmptyReviewsScraper:
        def scrape_reviews_data(self, *_: object) -> dict[str, list[str]]:
            return {"reviews": []}

    adapter = _primary_adapter()
    adapter._reviews_scraper_type = EmptyReviewsScraper  # type: ignore[attr-defined]
    adapter._reviews_scraper = EmptyReviewsScraper()  # type: ignore[attr-defined]

    assert adapter.get_reviews("com.example.app", "en", "us", 10).reviews == ()


def test_primary_adapter_rejects_unpinned_gplay_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gplay_module, "version", lambda _: "9.9.9")

    with pytest.raises(RuntimeError, match="gplay-scraper==1.0.6"):
        GPlayScraperAdapter(FakeControlledClient())  # type: ignore[arg-type]


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


def test_secondary_accepts_1000_reviews_and_preserves_positions() -> None:
    reviews = [
        {
            "reviewId": f"r-{index}",
            "userName": "Ada",
            "score": 5,
            "content": "ok",
            "at": NOW,
            "thumbsUpCount": 0,
        }
        for index in range(1000)
    ]
    adapter = GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {}, review_fetcher=lambda **_: reviews, now=lambda: NOW
    )

    normalized = adapter.get_reviews("com.example.app", "en", "us", 1000)

    assert len(normalized.reviews) == 1000
    assert normalized.reviews[-1].position == 1000


@pytest.mark.parametrize("adapter_name", ["primary", "secondary"])
def test_both_adapters_isolate_one_malformed_review_after_100_valid_reviews(
    adapter_name: str,
) -> None:
    if adapter_name == "primary":
        items = [_review_item(review_id=f"review-{index}") for index in range(100)]
        items.append(_review_item(review_id="malformed", content=None))
        adapter = _primary_review_adapter([items])
    else:
        records = [_raw_review(index) for index in range(100)]
        records.append(_raw_review(100, content=None))
        adapter = _secondary_review_adapter(records)

    normalized = adapter.get_reviews("org.telegram.messenger", "en", "us", 1000)

    assert len(normalized.reviews) == 100
    assert normalized.reviews[-1].position == 100


@pytest.mark.parametrize("adapter_name", ["primary", "secondary"])
@pytest.mark.parametrize(
    ("primary_field", "secondary_field", "invalid_value"),
    [
        ("content", "content", None),
        ("author", "userName", None),
        ("review_id", "reviewId", None),
        ("score", "score", 6),
        ("thumbs_up_count", "thumbsUpCount", -1),
        ("timestamp", "at", "invalid-timestamp"),
    ],
)
def test_both_adapters_isolate_expected_individual_field_failures(
    adapter_name: str,
    primary_field: str,
    secondary_field: str,
    invalid_value: object,
) -> None:
    if adapter_name == "primary":
        items = [_review_item(review_id=f"review-{index}") for index in range(19)]
        items.insert(5, _review_item(**{primary_field: invalid_value}))
        adapter = _primary_review_adapter([items])
    else:
        records = [_raw_review(index) for index in range(19)]
        records.insert(5, _raw_review(99, **{secondary_field: invalid_value}))
        adapter = _secondary_review_adapter(records)

    normalized = adapter.get_reviews("com.example.app", "en", "us", 20)

    assert len(normalized.reviews) == 19
    assert 6 not in {review.position for review in normalized.reviews}
    assert (
        next(
            review.position
            for review in normalized.reviews
            if review.external_review_id == "review-5"
        )
        == 7
    )


@pytest.mark.parametrize("adapter_name", ["primary", "secondary"])
def test_both_adapters_keep_source_position_1000_after_an_earlier_skip(
    adapter_name: str,
) -> None:
    if adapter_name == "primary":
        items = [_review_item(review_id=f"review-{index}") for index in range(1000)]
        items[499] = _review_item(review_id="malformed", content=None)
        adapter = _primary_review_adapter([items])
    else:
        records = [_raw_review(index) for index in range(1000)]
        records[499] = _raw_review(499, content=None)
        adapter = _secondary_review_adapter(records)

    normalized = adapter.get_reviews("com.example.app", "en", "us", 1000)

    assert len(normalized.reviews) == 999
    assert normalized.reviews[-1].position == 1000
    assert max(review.position for review in normalized.reviews) == 1000


@pytest.mark.parametrize("adapter_name", ["primary", "secondary"])
def test_both_adapters_reject_a_completely_invalid_nonempty_batch(
    adapter_name: str,
) -> None:
    if adapter_name == "primary":
        adapter = _primary_review_adapter(
            [[_review_item(review_id=f"bad-{index}", content=None) for index in range(20)]]
        )
    else:
        adapter = _secondary_review_adapter(
            [_raw_review(index, content=None) for index in range(20)]
        )

    with pytest.raises(SchemaFailure, match="corruption threshold"):
        adapter.get_reviews("com.example.app", "en", "us", 20)


@pytest.mark.parametrize("adapter_name", ["primary", "secondary"])
def test_both_adapters_reject_more_than_five_percent_invalid_records(
    adapter_name: str,
) -> None:
    if adapter_name == "primary":
        items = [_review_item(review_id=f"review-{index}") for index in range(18)]
        items.extend(
            [
                _review_item(review_id="bad-1", content=None),
                _review_item(review_id="bad-2", content=None),
            ]
        )
        adapter = _primary_review_adapter([items])
    else:
        records = [_raw_review(index) for index in range(18)]
        records.extend([_raw_review(18, content=None), _raw_review(19, content=None)])
        adapter = _secondary_review_adapter(records)

    with pytest.raises(SchemaFailure, match="corruption threshold"):
        adapter.get_reviews("com.example.app", "en", "us", 20)


def test_primary_isolates_malformed_item_on_a_later_page() -> None:
    first_page = [_review_item(review_id=f"review-{index}") for index in range(20)]
    second_page = [_review_item(review_id="bad-later", content=None)] + [
        _review_item(review_id=f"review-{index}") for index in range(20, 40)
    ]

    normalized = _primary_review_adapter([first_page, second_page]).get_reviews(
        "com.example.app", "en", "us", 41
    )

    assert len(normalized.reviews) == 40
    assert 21 not in {review.position for review in normalized.reviews}
    assert normalized.reviews[20].position == 22


@pytest.mark.parametrize("adapter_name", ["primary", "secondary"])
def test_invalid_review_logs_are_structured_and_do_not_contain_review_data(
    adapter_name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_author = "PRIVATE-AUTHOR-DO-NOT-LOG"
    secret_review_id = "PRIVATE-REVIEW-ID-DO-NOT-LOG"
    if adapter_name == "primary":
        items = [_review_item(review_id=f"review-{index}") for index in range(100)]
        items.append(
            _review_item(
                review_id=secret_review_id,
                author=secret_author,
                content=None,
            )
        )
        adapter = _primary_review_adapter([items])
        expected_adapter = "gplay-scraper"
    else:
        records = [_raw_review(index) for index in range(100)]
        records.append(
            _raw_review(
                100,
                reviewId=secret_review_id,
                userName=secret_author,
                content=None,
            )
        )
        adapter = _secondary_review_adapter(records)
        expected_adapter = "google-play-scraper"

    with caplog.at_level(logging.WARNING):
        adapter.get_reviews("org.telegram.messenger", "en", "us", 1000)

    diagnostic = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "crawler.review.invalid_record"
        and getattr(record, "adapter_name", None) == expected_adapter
    )
    assert diagnostic.package_name == "org.telegram.messenger"
    assert diagnostic.requested_count == 1000
    assert diagnostic.received_count == 101
    assert diagnostic.accepted_count == 100
    assert diagnostic.skipped_count == 1
    assert diagnostic.source_position == 101
    assert diagnostic.field_names == ("content",)
    assert diagnostic.validation_error_types == ("string_type",)
    diagnostic_data = repr(diagnostic.__dict__)
    assert secret_author not in caplog.text
    assert secret_review_id not in caplog.text
    assert secret_author not in diagnostic_data
    assert secret_review_id not in diagnostic_data


def test_secondary_default_fetcher_paginates_1000_newest_reviews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google_play_scraper.constants.element import ElementSpecs
    from google_play_scraper.features import reviews as reviews_module

    calls: list[tuple[int, object]] = []
    next_index = 0

    def fetch_items(
        _url: str,
        _app_id: str,
        _sort: int,
        count: int,
        _score: object,
        _device: object,
        token: object,
    ) -> tuple[list[list[object]], str | None]:
        nonlocal next_index
        calls.append((count, token))
        items = [
            [f"review-{index}", None, None, None, None, [REVIEW_TIMESTAMP]]
            for index in range(next_index, next_index + count)
        ]
        next_index += count
        return items, f"next-page-{next_index}" if next_index < 1000 else None

    monkeypatch.setattr(reviews_module, "MAX_COUNT_EACH_FETCH", 200)
    monkeypatch.setattr(reviews_module, "_fetch_review_items", fetch_items)
    monkeypatch.setattr(ElementSpecs, "Review", {"reviewId": FakeSpec(0)})

    reviews = google_play_module._default_review_fetcher(
        app_id="com.example.app",
        lang="en",
        country="us",
        count=1000,
    )

    assert [count for count, _ in calls] == [200, 200, 200, 200, 200]
    assert [review["reviewId"] for review in reviews] == [
        f"review-{index}" for index in range(1000)
    ]


def test_secondary_isolates_malformed_item_on_a_later_pagination_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google_play_scraper.constants.element import ElementSpecs
    from google_play_scraper.features import reviews as reviews_module

    calls = 0

    def fetch_items(*_: object) -> tuple[list[list[Any]], str | None]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                [_review_item(review_id=f"review-{index}") for index in range(20)],
                "page-2",
            )
        return (
            [_review_item(review_id="bad-later", content=None)]
            + [_review_item(review_id=f"review-{index}") for index in range(20, 39)],
            None,
        )

    monkeypatch.setattr(reviews_module, "MAX_COUNT_EACH_FETCH", 20)
    monkeypatch.setattr(reviews_module, "_fetch_review_items", fetch_items)
    monkeypatch.setattr(
        ElementSpecs,
        "Review",
        {
            "reviewId": FakeSpec(0),
            "userName": FakeSpec(1),
            "score": FakeSpec(2),
            "content": FakeSpec(4),
            "thumbsUpCount": FakeSpec(6),
        },
    )
    adapter = GooglePlayScraperAdapter(app_fetcher=lambda **_: {}, now=lambda: NOW)

    normalized = adapter.get_reviews("com.example.app", "en", "us", 40)

    assert calls == 2
    assert len(normalized.reviews) == 39
    assert 21 not in {review.position for review in normalized.reviews}
    assert normalized.reviews[20].position == 22


def test_secondary_rejects_repeated_pagination_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google_play_scraper.constants.element import ElementSpecs
    from google_play_scraper.features import reviews as reviews_module

    next_index = 0

    def fetch_items(*_: object) -> tuple[list[list[Any]], str]:
        nonlocal next_index
        items = [
            _review_item(review_id=f"review-{index}")
            for index in range(next_index, next_index + 20)
        ]
        next_index += 20
        return items, "repeated-token"

    monkeypatch.setattr(reviews_module, "MAX_COUNT_EACH_FETCH", 20)
    monkeypatch.setattr(reviews_module, "_fetch_review_items", fetch_items)
    monkeypatch.setattr(ElementSpecs, "Review", {"reviewId": FakeSpec(0)})

    with pytest.raises(ParseFailure, match="pagination token"):
        google_play_module._default_review_fetcher(
            app_id="com.example.app",
            lang="en",
            country="us",
            count=60,
        )


def test_secondary_rejects_systemically_invalid_response_set() -> None:
    adapter = GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {},
        review_fetcher=lambda **_: None,  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(ParseFailure, match="invalid response set"):
        adapter.get_reviews("com.example.app", "en", "us", 20)


@pytest.mark.parametrize("adapter_name", ["primary", "secondary"])
def test_adapters_do_not_swallow_unexpected_extractor_failures(
    adapter_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SystemicallyBrokenSpec:
        def extract_content(self, _: object) -> object:
            raise RuntimeError("extractor implementation is broken")

    if adapter_name == "primary":
        adapter = _primary_review_adapter([[_review_item()]])
        adapter._review_specs = {  # type: ignore[attr-defined]
            "reviewId": SystemicallyBrokenSpec()
        }
    else:
        from google_play_scraper.constants.element import ElementSpecs
        from google_play_scraper.features import reviews as reviews_module

        monkeypatch.setattr(
            reviews_module,
            "_fetch_review_items",
            lambda *_: ([_review_item()], None),
        )
        monkeypatch.setattr(
            ElementSpecs,
            "Review",
            {"reviewId": SystemicallyBrokenSpec()},
        )
        adapter = GooglePlayScraperAdapter(app_fetcher=lambda **_: {})

    with pytest.raises(RuntimeError, match="extractor implementation"):
        adapter.get_reviews("com.example.app", "en", "us", 20)


def test_secondary_uses_last_updated_on_when_updated_is_absent() -> None:
    adapter = GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {
            "minInstalls": 1,
            "score": 4,
            "ratings": 1,
            "reviews": 1,
            "lastUpdatedOn": "Sep 05, 2026",
            "version": "1",
        },
        review_fetcher=lambda **_: [],
        now=lambda: NOW,
    )

    result = adapter.get_app("com.example.app", "en", "us")

    assert result.store_updated_on == date(2026, 9, 5)


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (REVIEW_TIMESTAMP, datetime.fromtimestamp(REVIEW_TIMESTAMP, UTC)),
        (float(REVIEW_TIMESTAMP), datetime.fromtimestamp(REVIEW_TIMESTAMP, UTC)),
        ("2026-09-05T10:00:00Z", datetime(2026, 9, 5, 10, tzinfo=UTC)),
        (
            datetime.fromisoformat("2026-09-05T13:30:00+03:30"),
            datetime(2026, 9, 5, 10, tzinfo=UTC),
        ),
    ],
)
def test_secondary_review_timestamp_normalization_matrix(
    timestamp: object,
    expected: datetime,
) -> None:
    adapter = GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {},
        review_fetcher=lambda **_: [
            {
                "reviewId": "r",
                "userName": "Ada",
                "score": 5,
                "content": "ok",
                "at": timestamp,
                "thumbsUpCount": 0,
            }
        ],
        now=lambda: NOW,
    )

    result = adapter.get_reviews("com.example.app", "en", "us", 1)

    assert result.reviews[0].source_at == expected
    assert result.reviews[0].source_at.tzinfo is UTC


@pytest.mark.parametrize("timestamp", [None, datetime(2026, 9, 5, 10)])
def test_secondary_rejects_missing_or_naive_review_timestamps(timestamp: object) -> None:
    adapter = GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {},
        review_fetcher=lambda **_: [
            {
                "reviewId": "r",
                "userName": "Ada",
                "score": 5,
                "content": "ok",
                "at": timestamp,
                "thumbsUpCount": 0,
            }
        ],
    )

    with pytest.raises(SchemaFailure):
        adapter.get_reviews("com.example.app", "en", "us", 1)


@pytest.mark.parametrize("limit", [0, -1])
def test_secondary_review_limit_must_be_positive(limit: int) -> None:
    adapter = GooglePlayScraperAdapter(app_fetcher=lambda **_: {}, review_fetcher=lambda **_: [])

    with pytest.raises(ValueError, match="positive"):
        adapter.get_reviews("com.example.app", "en", "us", limit)


def test_secondary_caps_requested_review_count_before_fetching() -> None:
    counts: list[int] = []

    def fetch_reviews(**kwargs: object) -> list[dict[str, Any]]:
        counts.append(int(kwargs["count"]))
        return []

    adapter = GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {},
        review_fetcher=fetch_reviews,
    )

    adapter.get_reviews("com.example.app", "en", "us", 1001)

    assert counts == [1000]


def test_secondary_missing_ad_supported_defaults_to_false() -> None:
    adapter = GooglePlayScraperAdapter(
        app_fetcher=lambda **_: {
            "minInstalls": 1,
            "score": 4,
            "ratings": 1,
            "reviews": 1,
        },
        review_fetcher=lambda **_: [],
        now=lambda: NOW,
    )

    assert adapter.get_app("com.example.app", "en", "us").ad_supported is False


def test_secondary_wrapper_has_no_cross_request_state_under_concurrent_calls() -> None:
    def fetch_app(**kwargs: object) -> dict[str, object]:
        return {
            "minInstalls": 1,
            "score": 4,
            "ratings": 1,
            "reviews": 1,
            "version": str(kwargs["app_id"]),
        }

    def fetch_reviews(**kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "reviewId": str(kwargs["app_id"]),
                "userName": "Ada",
                "score": 5,
                "content": "ok",
                "at": NOW,
                "thumbsUpCount": 0,
            }
        ]

    adapter = GooglePlayScraperAdapter(
        app_fetcher=fetch_app,
        review_fetcher=fetch_reviews,
        now=lambda: NOW,
    )
    packages = [f"com.example.app{index}" for index in range(20)]

    def fetch(package: str) -> tuple[str | None, str]:
        app = adapter.get_app(package, "en", "us")
        review = adapter.get_reviews(package, "en", "us", 1).reviews[0]
        return app.version, review.external_review_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(fetch, packages))

    assert results == [(package, package) for package in packages]


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
    assert (
        client.fetch_reviews_batch("com.example.app", "en", "us", 2, 37, "next-page") == "reviews"
    )

    assert transport.calls[0][0] == "GET"
    assert transport.calls[0][1] == "https://play.google.test/store/apps/details"
    assert transport.calls[0][2]["headers"] == {"user-agent": "test"}
    assert transport.calls[0][2]["params"] == {
        "id": "com.example.app",
        "hl": "en",
        "gl": "us",
    }
    assert transport.calls[1][0] == "POST"
    assert transport.calls[1][1] == ("https://play.google.test/_/PlayStoreUi/data/batchexecute")
    assert transport.calls[1][2]["params"] == {"hl": "en", "gl": "us"}
    assert transport.calls[1][2]["headers"]["content-type"] == ("application/x-www-form-urlencoded")

    form = parse_qs(transport.calls[1][2]["data"])
    assert "f.req" in form
    rpc = json.loads(form["f.req"][0])[0][0]
    assert rpc[0] == "oCPfdb"
    nested = json.loads(rpc[1])
    assert nested[2][0] == "com.example.app"
    assert nested[1][1] == 2
    assert nested[1][2] == [37, None, "next-page"]


def test_controlled_client_app_request_without_locale_sends_only_id() -> None:
    transport = FakeTransport([TransportResponse(200, "app", {})])
    client = _controlled(transport)

    assert client.fetch_app_page_no_locale("com.example.app") == "app"

    assert transport.calls == [
        (
            "GET",
            "https://play.google.test/store/apps/details",
            {
                "headers": {"user-agent": "test"},
                "data": None,
                "params": {"id": "com.example.app"},
            },
        )
    ]


def test_controlled_client_empty_headers_fall_back_to_defaults() -> None:
    transport = FakeTransport([TransportResponse(200, "ok", {})])
    client = _controlled(transport)

    client._request("GET", "https://play.google.test/custom", headers={})

    assert transport.calls[0][2]["headers"] == {"user-agent": "test"}


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
