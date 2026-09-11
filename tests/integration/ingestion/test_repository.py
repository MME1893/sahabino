from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from sahabino.app_registry.models import Application
from sahabino.crawler.infrastructure.persistence.models import CrawlRun, CrawlTask
from sahabino.db.sync_session import create_sync_session_factory
from sahabino.ingestion.exceptions import IngestionConsistencyError
from sahabino.ingestion.handlers import handle_app_stats, handle_review_observed
from sahabino.ingestion.models import (
    IngestedEvent,
    PlaystoreAppSnapshot,
    Review,
    ReviewObservation,
)
from sahabino.ingestion.repository import IngestionRepository
from sahabino.messaging.playstore_events import (
    AppStatsCollectedV1,
    ReviewObservedV1,
    app_stats_envelope,
    review_observed_envelope,
)


def _factory(database_url: str) -> sessionmaker[Session]:
    return create_sync_session_factory(database_url)


def _application(factory: sessionmaker[Session]) -> Application:
    with factory.begin() as session:
        application = Application(
            name="Integration App",
            package_name=f"com.example.{uuid4().hex}",
        )
        session.add(application)
        session.flush()
        application_id = application.id
    with factory() as session:
        return session.get_one(Application, application_id)


def _task(factory: sessionmaker[Session], application_id: UUID, task_type: str) -> CrawlTask:
    with factory.begin() as session:
        run = CrawlRun(trigger_type="manual", status="running")
        session.add(run)
        session.flush()
        task = CrawlTask(
            crawl_run_id=run.id,
            application_id=application_id,
            task_type=task_type,
            status="running",
            language_code="en",
            country_code="us",
        )
        session.add(task)
        session.flush()
        task_id = task.id
    with factory() as session:
        return session.get_one(CrawlTask, task_id)


def _app_payload(application: Application, task: CrawlTask) -> AppStatsCollectedV1:
    return AppStatsCollectedV1(
        crawl_task_id=task.id,
        application_id=application.id,
        package_name=application.package_name,
        collected_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        min_installs=1234,
        score=4.4,
        ratings_count=300,
        reviews_count=120,
        store_updated_on=date(2026, 9, 10),
        version="2.0",
        ad_supported=True,
        source_adapter="google-play-scraper",
    )


def _review_payload(
    application: Application,
    task: CrawlTask,
    *,
    observed_at: datetime,
    score: int,
    content: str,
    thumbs_up_count: int = 1,
) -> ReviewObservedV1:
    return ReviewObservedV1(
        crawl_task_id=task.id,
        application_id=application.id,
        package_name=application.package_name,
        observed_at=observed_at,
        position=1,
        external_review_id="stable-review-id",
        source_at=observed_at,
        author_name="Reviewer",
        thumbs_up_count=thumbs_up_count,
        score=score,
        content=content,
        source_adapter="google-play-scraper",
    )


def test_ingestion_schema_and_migration_head(crawler_database_url: str) -> None:
    engine = _factory(crawler_database_url).kw["bind"]
    schema = inspect(engine)

    assert {
        "ingested_events",
        "playstore_app_snapshots",
        "reviews",
        "review_observations",
    }.issubset(schema.get_table_names())
    assert schema.get_pk_constraint("ingested_events")["constrained_columns"] == ["event_id"]
    assert set(schema.get_pk_constraint("review_observations")["constrained_columns"]) == {
        "crawl_task_id",
        "review_id",
    }
    assert {
        constraint["name"] for constraint in schema.get_unique_constraints("ingested_events")
    } == {"uq_ingested_events_topic_partition_offset"}
    assert {constraint["name"] for constraint in schema.get_unique_constraints("reviews")} == {
        "uq_reviews_application_id_external_review_id"
    }
    assert len(schema.get_foreign_keys("playstore_app_snapshots")) == 2
    assert len(schema.get_foreign_keys("review_observations")) == 2
    assert len(schema.get_check_constraints("ingested_events")) == 3
    assert len(schema.get_check_constraints("playstore_app_snapshots")) == 4
    assert len(schema.get_check_constraints("reviews")) == 3
    assert len(schema.get_check_constraints("review_observations")) == 3

    with engine.connect() as connection:
        assert connection.scalar(select(func.max(IngestedEvent.schema_version))) is None
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == ("20260911_0003")


def test_app_snapshot_and_event_claim_are_idempotent(crawler_database_url: str) -> None:
    factory = _factory(crawler_database_url)
    application = _application(factory)
    task = _task(factory, application.id, "app_details")
    payload = _app_payload(application, task)
    event = app_stats_envelope(payload)

    with factory.begin() as session:
        repository = IngestionRepository(session)
        assert repository.claim_event(
            event_id=event.event_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            topic="playstore.app-stats.v1",
            partition=0,
            offset=1,
        )
        handle_app_stats(event, repository)

    with factory.begin() as session:
        repository = IngestionRepository(session)
        assert not repository.claim_event(
            event_id=event.event_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            topic="playstore.app-stats.v1",
            partition=0,
            offset=2,
        )
        repository.insert_app_snapshot(payload)

    with factory() as session:
        snapshot = session.scalars(select(PlaystoreAppSnapshot)).one()
        assert snapshot.application_id == application.id
        assert snapshot.crawl_task_id == task.id
        assert snapshot.min_installs == 1234
        assert session.scalar(select(func.count()).select_from(IngestedEvent)) == 1
        assert session.scalar(select(func.count()).select_from(PlaystoreAppSnapshot)) == 1


def test_review_current_state_ordering_and_observation_history(
    crawler_database_url: str,
) -> None:
    factory = _factory(crawler_database_url)
    application = _application(factory)
    equal_time = datetime(2026, 9, 11, 16, tzinfo=UTC)
    inputs = [
        (equal_time, 5, "initial", 1),
        (equal_time, 4, "equal timestamp wins", 2),
        (datetime(2026, 9, 11, 18, tzinfo=UTC), 5, "newest", 3),
        (datetime(2026, 9, 11, 14, tzinfo=UTC), 1, "older", 0),
    ]

    for offset, (observed_at, score, content, thumbs) in enumerate(inputs):
        task = _task(factory, application.id, "reviews")
        event = review_observed_envelope(
            _review_payload(
                application,
                task,
                observed_at=observed_at,
                score=score,
                content=content,
                thumbs_up_count=thumbs,
            )
        )
        with factory.begin() as session:
            repository = IngestionRepository(session)
            assert repository.claim_event(
                event_id=event.event_id,
                event_type=event.event_type,
                schema_version=event.schema_version,
                topic="playstore.review-observed.v1",
                partition=0,
                offset=offset,
            )
            handle_review_observed(event, repository)

    with factory() as session:
        review = session.scalars(select(Review)).one()
        assert review.content == "newest"
        assert review.score == 5
        assert review.thumbs_up_count == 3
        assert review.first_observed_at == datetime(2026, 9, 11, 14, tzinfo=UTC)
        assert review.last_observed_at == datetime(2026, 9, 11, 18, tzinfo=UTC)
        observations = session.scalars(select(ReviewObservation)).all()
        assert {
            (
                observation.observed_at,
                observation.score,
                observation.thumbs_up_count,
            )
            for observation in observations
        } == {
            (datetime(2026, 9, 11, 14, tzinfo=UTC), 1, 0),
            (datetime(2026, 9, 11, 16, tzinfo=UTC), 5, 1),
            (datetime(2026, 9, 11, 16, tzinfo=UTC), 4, 2),
            (datetime(2026, 9, 11, 18, tzinfo=UTC), 5, 3),
        }


def test_duplicate_logical_review_observation_is_safe(crawler_database_url: str) -> None:
    factory = _factory(crawler_database_url)
    application = _application(factory)
    task = _task(factory, application.id, "reviews")
    payload = _review_payload(
        application,
        task,
        observed_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        score=5,
        content="same",
    )

    for offset in (1, 2):
        event = review_observed_envelope(payload)
        with factory.begin() as session:
            repository = IngestionRepository(session)
            assert repository.claim_event(
                event_id=event.event_id,
                event_type=event.event_type,
                schema_version=event.schema_version,
                topic="playstore.review-observed.v1",
                partition=0,
                offset=offset,
            )
            handle_review_observed(event, repository)

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Review)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewObservation)) == 1
        assert session.scalar(select(func.count()).select_from(IngestedEvent)) == 2


def test_crawl_task_consistency_failure_rolls_back_event_claim(
    crawler_database_url: str,
) -> None:
    factory = _factory(crawler_database_url)
    application = _application(factory)
    wrong_type_task = _task(factory, application.id, "reviews")
    payload = _app_payload(application, wrong_type_task)
    event = app_stats_envelope(payload)

    with pytest.raises(IngestionConsistencyError), factory.begin() as session:
        repository = IngestionRepository(session)
        assert repository.claim_event(
            event_id=event.event_id,
            event_type=event.event_type,
            schema_version=event.schema_version,
            topic="playstore.app-stats.v1",
            partition=0,
            offset=1,
        )
        handle_app_stats(event, repository)

    with factory() as session:
        assert session.get(IngestedEvent, event.event_id) is None
        assert session.scalar(select(func.count()).select_from(PlaystoreAppSnapshot)) == 0


def test_crawl_task_application_mismatch_fails(crawler_database_url: str) -> None:
    factory = _factory(crawler_database_url)
    application = _application(factory)
    other_application = _application(factory)
    task = _task(factory, other_application.id, "app_details")

    with factory() as session:
        repository = IngestionRepository(session)
        with pytest.raises(IngestionConsistencyError):
            repository.validate_crawl_task(
                crawl_task_id=task.id,
                application_id=application.id,
                expected_task_type="app_details",
            )
