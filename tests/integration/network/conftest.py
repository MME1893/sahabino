from __future__ import annotations

import asyncio
import os
import selectors
import socket
import sys
import time
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.kafka import KafkaContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

from sahabino.common.config import Settings, get_settings
from sahabino.messaging.admin import delete_topics, ensure_topics
from sahabino.messaging.exceptions import TopicProvisioningError
from sahabino.messaging.producer import KafkaProducer
from sahabino.messaging.topics import (
    NETWORK_ANALYSIS_COLLECTED_TOPIC,
    NETWORK_CAPTURE_READY_TOPIC,
    TopicTopology,
)
from sahabino.network.publisher import KafkaNetworkPublisher
from sahabino.network.storage import S3ObjectStorage
from tests.integration.crawler.conftest import (
    KAFKA_IMAGE,
    POSTGRES_IMAGE,
    _configured_database,
    psycopg_dsn,
    psycopg_sqlalchemy_url,
)

SEAWEEDFS_IMAGE = "chrislusf/seaweedfs:4.46"
NETWORK_TOPICS = (NETWORK_CAPTURE_READY_TOPIC, NETWORK_ANALYSIS_COLLECTED_TOPIC)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _selector_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


@pytest.fixture(scope="session")
def network_database_url() -> Iterator[str]:
    supplied = os.getenv("TEST_DATABASE_URL")
    if supplied:
        yield from _configured_database(psycopg_sqlalchemy_url(supplied))
        return
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        yield from _configured_database(psycopg_sqlalchemy_url(postgres.get_connection_url()))


@pytest.fixture(scope="session")
def network_kafka_bootstrap_servers() -> Iterator[str]:
    supplied = os.getenv("TEST_KAFKA_BOOTSTRAP_SERVERS")
    if supplied:
        yield supplied
        return
    container = KafkaContainer(KAFKA_IMAGE).with_kraft()
    container.with_env("KAFKA_AUTO_CREATE_TOPICS_ENABLE", "false")
    with container as kafka:
        yield kafka.get_bootstrap_server()


def _wait_for_tcp(host: str, port: int, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for S3 endpoint {host}:{port}")


@pytest.fixture(scope="session")
def network_s3_endpoint() -> Iterator[str]:
    supplied = os.getenv("TEST_S3_ENDPOINT_URL")
    if supplied:
        yield supplied
        return
    container = (
        DockerContainer(SEAWEEDFS_IMAGE)
        .with_exposed_ports(8333)
        .with_command("server -s3 -dir=/data -s3.port=8333")
    )
    with container:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(8333))
        _wait_for_tcp(host, port)
        yield f"http://{host}:{port}"


@pytest.fixture
def network_topics(network_kafka_bootstrap_servers: str) -> Iterator[tuple[str, str]]:
    ensure_topics(
        network_kafka_bootstrap_servers,
        NETWORK_TOPICS,
        TopicTopology(partitions=1, replication_factor=1),
    )
    try:
        yield NETWORK_TOPICS
    finally:
        try:
            delete_topics(network_kafka_bootstrap_servers, NETWORK_TOPICS)
        except TopicProvisioningError as error:
            warnings.warn(f"network topic cleanup failed: {error}", RuntimeWarning, stacklevel=2)


@pytest.fixture(autouse=True)
def clean_network_database(network_database_url: str) -> Iterator[None]:
    statement = (
        "TRUNCATE TABLE network_analysis_results, network_captures, ingested_events, "
        "crawl_tasks, crawl_runs, application_categories, applications RESTART IDENTITY CASCADE"
    )
    dsn = psycopg_dsn(network_database_url)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(statement)
    yield
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(statement)


@pytest.fixture
def network_settings(
    network_database_url: str,
    network_kafka_bootstrap_servers: str,
    network_s3_endpoint: str,
) -> Settings:
    return Settings(
        database_url=network_database_url,
        kafka_bootstrap_servers=network_kafka_bootstrap_servers,
        object_storage_endpoint_url=network_s3_endpoint,
        object_storage_public_endpoint_url=network_s3_endpoint,
        object_storage_bucket="sahabino-network-integration",
        object_storage_access_key="integration",
        object_storage_secret_key="integration-secret",
        kafka_topic_partitions=1,
        kafka_topic_replication_factor=1,
    )


@pytest.fixture
def network_storage(network_settings: Settings) -> Iterator[S3ObjectStorage]:
    storage = S3ObjectStorage(network_settings)
    storage.ensure_bucket()
    existing = storage._client.list_objects_v2(  # type: ignore[attr-defined]
        Bucket=network_settings.object_storage_bucket
    ).get("Contents", [])
    for item in existing:
        storage.delete_object(str(item["Key"]))
    try:
        yield storage
    finally:
        remaining = storage._client.list_objects_v2(  # type: ignore[attr-defined]
            Bucket=network_settings.object_storage_bucket
        ).get("Contents", [])
        for item in remaining:
            storage.delete_object(str(item["Key"]))


@pytest.fixture
def network_api_client(
    network_settings: Settings,
    network_storage: S3ObjectStorage,
    network_topics: tuple[str, str],
) -> Iterator[TestClient]:
    _ = network_topics
    from sahabino.db.session import get_session
    from sahabino.main import create_app
    from sahabino.network.dependencies import get_network_publisher, get_object_storage

    engine = create_async_engine(network_settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    publisher = KafkaNetworkPublisher(KafkaProducer.from_settings(network_settings))

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    application = create_app()
    application.dependency_overrides[get_session] = override_session
    application.dependency_overrides[get_settings] = lambda: network_settings
    application.dependency_overrides[get_object_storage] = lambda: network_storage
    application.dependency_overrides[get_network_publisher] = lambda: publisher
    backend = {"loop_factory": _selector_loop_factory} if sys.platform == "win32" else None
    try:
        with TestClient(application, backend_options=backend) as client:
            yield client
    finally:
        publisher.close()
        if sys.platform == "win32":
            asyncio.run(engine.dispose(), loop_factory=_selector_loop_factory)
        else:
            asyncio.run(engine.dispose())


@pytest.fixture
def create_network_application(
    network_api_client: TestClient,
) -> Callable[..., dict[str, Any]]:
    sequence = 0

    def create(**overrides: Any) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        payload: dict[str, Any] = {
            "name": f"Network Application {sequence}",
            "package_name": f"com.example.network{sequence}",
            "category_codes": ["messaging"],
            "primary_category_code": "messaging",
        }
        payload.update(overrides)
        response = network_api_client.post("/applications", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return create
