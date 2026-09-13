from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sahabino.ingestion.repository import IngestionRepository
from sahabino.network.models import NetworkCapture
from scripts.generate_network_fixture import build_fixture
from tests.integration.crawler.conftest import psycopg_dsn
from tests.unit.network.fakes import analysis_payload


def _registered_capture(
    client: TestClient,
    create_application: Any,
) -> dict[str, Any]:
    application = create_application(package_name="com.example.constraints")
    capture = build_fixture()
    response = client.post(
        "/network-captures",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "application_id": application["id"],
            "scenario": "upload",
            "filename": "constraints.pcapng",
            "capture_size_bytes": len(capture),
            "transfer_file_size_bytes": 20,
            "sha256": hashlib.sha256(capture).hexdigest(),
        },
    )
    assert response.status_code == 201
    return {"application": application, "capture": response.json(), "size": len(capture)}


def test_network_capture_database_checks_are_enforced(
    network_api_client: TestClient,
    create_network_application: Any,
    network_database_url: str,
) -> None:
    registered = _registered_capture(network_api_client, create_network_application)
    dsn = psycopg_dsn(network_database_url)

    with psycopg.connect(dsn) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "UPDATE network_captures SET capture_size_bytes=0 WHERE id=%s",
            (registered["capture"]["capture_id"],),
        )

    with psycopg.connect(dsn) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "UPDATE network_captures SET status='unknown' WHERE id=%s",
            (registered["capture"]["capture_id"],),
        )


def test_network_analysis_ratio_and_unique_capture_constraints_are_enforced(
    network_api_client: TestClient,
    create_network_application: Any,
    network_database_url: str,
) -> None:
    registered = _registered_capture(network_api_client, create_network_application)
    engine = create_engine(network_database_url)
    capture_id = UUID(registered["capture"]["capture_id"])
    application_id = UUID(registered["application"]["id"])
    with Session(engine) as session, session.begin():
        capture = session.get(NetworkCapture, capture_id)
        assert capture is not None
        capture.verified_sha256 = capture.expected_sha256
        payload = analysis_payload(application_id=application_id, capture_id=capture_id)
        payload = payload.model_copy(
            update={
                "analysis_id": capture.analysis_id,
                "package_name": registered["application"]["package_name"],
                "verified_sha256": capture.expected_sha256,
                "capture": payload.capture.model_copy(
                    update={
                        "capture_format": "pcapng",
                        "capture_size_bytes": registered["size"],
                        "transfer_file_size_bytes": 20,
                    }
                ),
            }
        )
        repository = IngestionRepository(session)
        repository.validate_network_analysis(payload)
        repository.insert_network_analysis(payload)

    with Session(engine) as session, pytest.raises(IntegrityError):
        IngestionRepository(session).insert_network_analysis(
            payload.model_copy(update={"analysis_id": uuid4()})
        )
        session.commit()

    with Session(engine) as session:
        existing = session.scalar(select(NetworkCapture).where(NetworkCapture.id == capture_id))
        assert existing is not None
        with pytest.raises(IntegrityError):
            session.execute(
                existing.__table__.update()
                .where(existing.__table__.c.id == capture_id)
                .values(analysis_attempt_count=-1)
            )
            session.commit()

    dsn = psycopg_dsn(network_database_url)
    with psycopg.connect(dsn) as connection, pytest.raises(psycopg.errors.CheckViolation):
        connection.execute(
            "UPDATE network_analysis_results SET tcp_retransmission_rate=2 WHERE capture_id=%s",
            (str(capture_id),),
        )

    engine.dispose()
