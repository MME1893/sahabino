from __future__ import annotations

import asyncio
import os
import selectors
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from testcontainers.community.kafka import KafkaContainer
from testcontainers.community.postgres import PostgresContainer

from sahabino.common.config import get_settings
from sahabino.messaging.admin import delete_topics, ensure_topics
from sahabino.messaging.exceptions import TopicProvisioningError
from sahabino.messaging.topics import (
    PLAYSTORE_APP_STATS_TOPIC,
    PLAYSTORE_REVIEW_OBSERVED_TOPIC,
    TopicTopology,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
POSTGRES_IMAGE = "postgres:16-alpine"
KAFKA_IMAGE = "confluentinc/cp-kafka:7.6.0"
CRAWLER_TOPICS = (PLAYSTORE_APP_STATS_TOPIC, PLAYSTORE_REVIEW_OBSERVED_TOPIC)


def psycopg_sqlalchemy_url(value: str) -> str:
    url = make_url(value)
    return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def psycopg_dsn(value: str) -> str:
    url = make_url(value)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _selector_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def _run_migrations(database_url: str) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def crawler_database_url() -> Iterator[str]:
    supplied_url = os.getenv("TEST_DATABASE_URL")
    if supplied_url:
        database_url = psycopg_sqlalchemy_url(supplied_url)
        yield from _configured_database(database_url)
        return
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = psycopg_sqlalchemy_url(postgres.get_connection_url())
        yield from _configured_database(database_url)


def _configured_database(database_url: str) -> Iterator[str]:
    previous = os.environ.get("SAHABINO_DATABASE_URL")
    os.environ["SAHABINO_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        _run_migrations(database_url)
        yield database_url
    finally:
        if previous is None:
            os.environ.pop("SAHABINO_DATABASE_URL", None)
        else:
            os.environ["SAHABINO_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean_crawler_database(crawler_database_url: str) -> Iterator[None]:
    dsn = psycopg_dsn(crawler_database_url)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE TABLE crawl_tasks, crawl_runs, application_categories, applications "
            "RESTART IDENTITY"
        )
    yield
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE TABLE crawl_tasks, crawl_runs, application_categories, applications "
            "RESTART IDENTITY"
        )


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
            warnings.warn(f"crawler topic cleanup failed: {error}", RuntimeWarning, stacklevel=2)
