from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sahabino.common.config import Settings, get_settings
from sahabino.db.session import get_session
from sahabino.messaging.producer import KafkaProducer
from sahabino.network.publisher import KafkaNetworkPublisher, NetworkPublisher
from sahabino.network.service import CaptureService
from sahabino.network.storage import ObjectStorage, S3ObjectStorage


@lru_cache
def get_object_storage() -> S3ObjectStorage:
    return S3ObjectStorage(get_settings())


@lru_cache
def get_network_publisher() -> KafkaNetworkPublisher:
    return KafkaNetworkPublisher(KafkaProducer.from_settings(get_settings()))


def get_capture_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    publisher: Annotated[NetworkPublisher, Depends(get_network_publisher)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CaptureService:
    return CaptureService(session, storage, publisher, settings)


def close_network_runtime() -> None:
    if get_network_publisher.cache_info().currsize:
        get_network_publisher().close()
    get_network_publisher.cache_clear()
    get_object_storage.cache_clear()
