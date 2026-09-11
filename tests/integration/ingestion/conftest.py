from __future__ import annotations

import os
import warnings
from collections.abc import Iterator

import psycopg
import pytest
from testcontainers.community.kafka import KafkaContainer
from testcontainers.community.postgres import PostgresContainer

from sahabino.messaging.admin import delete_topics, ensure_topics
from sahabino.messaging.exceptions import TopicProvisioningError
from sahabino.messaging.topics import TopicTopology
from tests.integration.crawler.conftest import (
    CRAWLER_TOPICS,
    KAFKA_IMAGE,
    POSTGRES_IMAGE,
    _configured_database,
    psycopg_dsn,
    psycopg_sqlalchemy_url,
)


@pytest.fixture(scope="session")
def crawler_database_url() -> Iterator[str]:
    supplied_url = os.getenv("TEST_DATABASE_URL")
    if supplied_url:
        yield from _configured_database(psycopg_sqlalchemy_url(supplied_url))
        return
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        yield from _configured_database(psycopg_sqlalchemy_url(postgres.get_connection_url()))


@pytest.fixture(scope="session")
def crawler_kafka_bootstrap_servers() -> Iterator[str]:
    supplied_servers = os.getenv("TEST_KAFKA_BOOTSTRAP_SERVERS")
    if supplied_servers:
        yield supplied_servers
        return
    container = KafkaContainer(KAFKA_IMAGE).with_kraft()
    container.with_env("KAFKA_AUTO_CREATE_TOPICS_ENABLE", "false")
    with container as kafka:
        yield kafka.get_bootstrap_server()


@pytest.fixture
def crawler_topics(crawler_kafka_bootstrap_servers: str) -> Iterator[tuple[str, str]]:
    topology = TopicTopology(partitions=1, replication_factor=1)
    ensure_topics(crawler_kafka_bootstrap_servers, CRAWLER_TOPICS, topology)
    try:
        yield CRAWLER_TOPICS
    finally:
        try:
            delete_topics(crawler_kafka_bootstrap_servers, CRAWLER_TOPICS)
        except TopicProvisioningError as error:
            warnings.warn(f"ingestion topic cleanup failed: {error}", RuntimeWarning, stacklevel=2)


@pytest.fixture(autouse=True)
def clean_ingestion_database(crawler_database_url: str) -> Iterator[None]:
    dsn = psycopg_dsn(crawler_database_url)
    statement = (
        "TRUNCATE TABLE ingested_events, crawl_tasks, crawl_runs, "
        "application_categories, applications RESTART IDENTITY CASCADE"
    )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(statement)
    yield
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(statement)
