from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sahabino.app_registry.models import Application
from sahabino.network.models import NetworkCapture, NetworkCaptureIdempotencyKey


class CaptureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def application_package_name(self, application_id: UUID) -> str | None:
        return cast(
            str | None,
            await self._session.scalar(
                select(Application.package_name).where(Application.id == application_id)
            ),
        )

    async def lock_object_key(self, object_key: str) -> None:
        """Serialize shared-object reference changes for this transaction."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:object_key, 0))"),
            {"object_key": object_key},
        )

    async def lock_idempotency_key(self, key: UUID) -> None:
        """Serialize a request key before taking any object-key lock."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"network-capture-idempotency:{key}"},
        )

    async def get(self, capture_id: UUID) -> NetworkCapture | None:
        return cast(
            NetworkCapture | None,
            await self._session.scalar(
                select(NetworkCapture).where(NetworkCapture.id == capture_id)
            ),
        )

    async def get_idempotency_key(self, key: UUID) -> NetworkCaptureIdempotencyKey | None:
        return cast(
            NetworkCaptureIdempotencyKey | None,
            await self._session.scalar(
                select(NetworkCaptureIdempotencyKey).where(
                    NetworkCaptureIdempotencyKey.idempotency_key == key
                )
            ),
        )

    async def get_logical(
        self, *, application_id: UUID, scenario: str, expected_sha256: str
    ) -> NetworkCapture | None:
        return cast(
            NetworkCapture | None,
            await self._session.scalar(
                select(NetworkCapture).where(
                    NetworkCapture.application_id == application_id,
                    NetworkCapture.scenario == scenario,
                    NetworkCapture.expected_sha256 == expected_sha256,
                )
            ),
        )

    async def get_verified_object(self, object_key: str) -> NetworkCapture | None:
        return cast(
            NetworkCapture | None,
            await self._session.scalar(
                select(NetworkCapture)
                .where(
                    NetworkCapture.object_key == object_key,
                    NetworkCapture.verified_sha256.is_not(None),
                    NetworkCapture.object_deleted_at.is_(None),
                )
                .order_by(NetworkCapture.analysis_finished_at.desc().nullslast())
                .limit(1)
            ),
        )

    async def list(
        self,
        *,
        application_id: UUID | None,
        scenario: str | None,
        status: str | None,
    ) -> list[NetworkCapture]:
        statement: Select[tuple[NetworkCapture]] = select(NetworkCapture)
        if application_id is not None:
            statement = statement.where(NetworkCapture.application_id == application_id)
        if scenario is not None:
            statement = statement.where(NetworkCapture.scenario == scenario)
        if status is not None:
            statement = statement.where(NetworkCapture.status == status)
        result = await self._session.scalars(
            statement.order_by(NetworkCapture.created_at.desc(), NetworkCapture.id)
        )
        return list(result)

    async def package_name_for_capture(self, capture: NetworkCapture) -> str:
        package_name = await self.application_package_name(capture.application_id)
        assert package_name is not None
        return package_name

    def add(self, capture: NetworkCapture) -> None:
        self._session.add(capture)

    def add_idempotency_key(self, mapping: NetworkCaptureIdempotencyKey) -> None:
        self._session.add(mapping)

    async def refresh(self, capture: NetworkCapture) -> None:
        await self._session.refresh(capture)

    async def flush(self) -> None:
        await self._session.flush()

    async def count_object_references(self, object_key: str) -> int:
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(NetworkCapture)
                .where(
                    NetworkCapture.object_key == object_key,
                    NetworkCapture.object_deleted_at.is_(None),
                )
            )
            or 0
        )

    async def captures_for_object(self, object_key: str) -> Sequence[NetworkCapture]:
        result = await self._session.scalars(
            select(NetworkCapture).where(NetworkCapture.object_key == object_key)
        )
        return list(result)

    async def mark_object_deleted(self, object_key: str, deleted_at: datetime) -> None:
        for capture in await self.captures_for_object(object_key):
            capture.object_deleted_at = deleted_at
