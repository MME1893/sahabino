from __future__ import annotations

import asyncio
import os
import selectors
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from sahabino.common.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[3]
POSTGRES_IMAGE = "postgres:16-alpine"


def _psycopg_sqlalchemy_url(value: str) -> str:
    """Return a SQLAlchemy URL that explicitly selects Psycopg 3."""
    url = make_url(value)
    return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _psycopg_dsn(value: str) -> str:
    """Return a driver-neutral DSN accepted by Psycopg 3 itself."""
    url = make_url(value)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _run_migrations(database_url: str) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    # ConfigParser treats percent signs in URL-encoded credentials as interpolation.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def _selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Create the selector loop required by Psycopg integration tests on Windows."""
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Use CI's PostgreSQL service or start an isolated local container."""
    supplied_url = os.getenv("TEST_DATABASE_URL")
    if supplied_url is not None:
        sqlalchemy_url = _psycopg_sqlalchemy_url(supplied_url)
        yield from _configured_database(sqlalchemy_url)
        return

    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        sqlalchemy_url = _psycopg_sqlalchemy_url(postgres.get_connection_url())
        yield from _configured_database(sqlalchemy_url)


def _configured_database(database_url: str) -> Iterator[str]:
    previous_url = os.environ.get("SAHABINO_DATABASE_URL")
    os.environ["SAHABINO_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        _run_migrations(database_url)
        yield database_url
    finally:
        if previous_url is None:
            os.environ.pop("SAHABINO_DATABASE_URL", None)
        else:
            os.environ["SAHABINO_DATABASE_URL"] = previous_url
        get_settings.cache_clear()


def _clear_applications(database_url: str) -> None:
    with psycopg.connect(_psycopg_dsn(database_url), autocommit=True) as connection:
        connection.execute(
            "TRUNCATE TABLE crawl_tasks, crawl_runs, application_categories, applications "
            "RESTART IDENTITY"
        )


@pytest.fixture(autouse=True)
def clean_applications(database_url: str) -> Iterator[None]:
    """Isolate tests while retaining category reference data from the migration."""
    _clear_applications(database_url)
    yield
    _clear_applications(database_url)


@pytest.fixture(scope="session")
def api_client(database_url: str) -> Iterator[TestClient]:
    from sahabino.db.session import get_session
    from sahabino.main import create_app

    test_engine = create_async_engine(database_url, pool_pre_ping=True)
    test_session_factory = async_sessionmaker(
        test_engine,
        expire_on_commit=False,
    )

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with test_session_factory() as session:
            yield session

    application = create_app()
    application.dependency_overrides[get_session] = override_get_session

    backend_options = {"loop_factory": _selector_loop_factory} if sys.platform == "win32" else None

    try:
        with TestClient(
            application,
            backend_options=backend_options,
        ) as client:
            yield client
    finally:
        if sys.platform == "win32":
            asyncio.run(
                test_engine.dispose(),
                loop_factory=_selector_loop_factory,
            )
        else:
            asyncio.run(test_engine.dispose())


@pytest.fixture
def db_connection(database_url: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(_psycopg_dsn(database_url)) as connection:
        yield connection


@pytest.fixture
def create_application(
    api_client: TestClient,
) -> Callable[..., dict[str, Any]]:
    sequence = 0

    def create(**overrides: Any) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        payload: dict[str, Any] = {
            "name": f"Application {sequence}",
            "package_name": f"com.example.application{sequence}",
            "category_codes": ["messaging", "social_network"],
            "primary_category_code": "messaging",
        }
        payload.update(overrides)
        response = api_client.post("/applications", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return create
