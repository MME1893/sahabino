from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from sahabino.app_registry.models import Application
from sahabino.messaging.network_events import capture_ready_envelope
from sahabino.network.models import NetworkCapture
from sahabino.network.worker import AnalyzerRepository
from tests.unit.network.fakes import capture_ready_payload


def test_only_one_fresh_attempt_is_claimed_and_stale_attempt_can_resume(
    network_database_url: str,
) -> None:
    engine = create_engine(network_database_url)
    application_id = uuid4()
    capture_id = uuid4()
    payload = capture_ready_payload(application_id=application_id, capture_id=capture_id)
    ready_event_id = uuid4()
    capture = NetworkCapture(
        id=capture_id,
        application_id=application_id,
        scenario=payload.scenario,
        original_filename="claim.pcap",
        capture_format=payload.capture_format,
        content_type="application/vnd.tcpdump.pcap",
        object_key=payload.object_key,
        expected_sha256=payload.expected_sha256,
        capture_size_bytes=payload.capture_size_bytes,
        transfer_file_size_bytes=payload.transfer_file_size_bytes,
        status="uploaded",
        ready_event_id=ready_event_id,
        analysis_id=payload.analysis_id,
        analysis_event_id=uuid4(),
        analysis_attempt_count=0,
        uploaded_at=payload.uploaded_at,
    )
    with Session(engine) as session, session.begin():
        session.add(
            Application(
                id=application_id,
                name="Claim Test",
                package_name=payload.package_name,
            )
        )
        session.add(capture)

    event = capture_ready_envelope(payload, event_id=ready_event_id)
    first_started_at = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        repository = AnalyzerRepository(session)
        repository.load_and_validate(event)
        assert repository.claim_attempt(
            capture_id,
            attempt_started_at=first_started_at,
            stale_before=first_started_at - timedelta(minutes=30),
        )

    with Session(engine) as session, session.begin():
        repository = AnalyzerRepository(session)
        repository.load_and_validate(event)
        assert not repository.claim_attempt(
            capture_id,
            attempt_started_at=first_started_at + timedelta(seconds=1),
            stale_before=first_started_at - timedelta(minutes=30),
        )

    stale_started_at = first_started_at - timedelta(hours=1)
    with Session(engine) as session, session.begin():
        stored = session.get(NetworkCapture, capture_id)
        assert stored is not None
        stored.analysis_started_at = stale_started_at

    resumed_at = first_started_at + timedelta(seconds=2)
    with Session(engine) as session, session.begin():
        repository = AnalyzerRepository(session)
        assert repository.claim_attempt(
            capture_id,
            attempt_started_at=resumed_at,
            stale_before=first_started_at - timedelta(minutes=30),
        )

    with Session(engine) as session, session.begin():
        AnalyzerRepository(session).release_attempt(capture_id, first_started_at)

    with Session(engine) as session:
        stored = session.get(NetworkCapture, capture_id)
        assert stored is not None
        assert stored.status == "analyzing"
        assert stored.analysis_attempt_count == 2
        assert stored.analysis_started_at == resumed_at
    engine.dispose()
