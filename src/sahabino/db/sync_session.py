from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def sync_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() == "postgresql" and url.drivername != "postgresql+psycopg":
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


def create_sync_engine(database_url: str) -> Engine:
    return create_engine(sync_database_url(database_url), pool_pre_ping=True)


def create_sync_session_factory(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(create_sync_engine(database_url), expire_on_commit=False)
