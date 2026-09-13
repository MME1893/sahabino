from __future__ import annotations

import json
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from confluent_kafka import Message
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from sahabino.app_registry.models import Application
from sahabino.messaging.consumer import KafkaConsumer
from sahabino.messaging.events import EventEnvelope, deserialize_event
from sahabino.messaging.exceptions import EventDeserializationError
from sahabino.messaging.network_events import (
    NETWORK_CAPTURE_READY_EVENT_TYPE,
    NETWORK_SCHEMA_VERSION,
    NetworkAnalysisCollectedV1,
    NetworkCaptureReadyV1,
)
from sahabino.messaging.topics import NETWORK_CAPTURE_READY_TOPIC
from sahabino.network.analyzer import AnalysisEngine, analyze_capture
from sahabino.network.exceptions import CaptureObjectMissingError, TerminalAnalysisError
from sahabino.network.models import NetworkCapture
from sahabino.network.publisher import NetworkPublisher
from sahabino.network.storage import ObjectStorage

logger = logging.getLogger(__name__)
ENVELOPE_FIELDS = frozenset({"event_id", "event_type", "schema_version", "occurred_at", "payload"})
POLL_TIMEOUT_SECONDS = 1.0


class InvalidAnalyzerMessage(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AnalyzerConsistencyError(Exception):
    pass


class AnalysisClaimUnavailable(Exception):
    """A fresh analyzer attempt already owns this capture."""


def decode_capture_ready(message: Message) -> EventEnvelope[NetworkCaptureReadyV1]:
    value = message.value()
    if value is None or not isinstance(value, bytes):
        raise InvalidAnalyzerMessage("missing_or_invalid_value")
    try:
        raw: Any = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise InvalidAnalyzerMessage("invalid_json") from None
    if not isinstance(raw, dict) or set(raw) != ENVELOPE_FIELDS:
        raise InvalidAnalyzerMessage("invalid_envelope")
    if raw.get("event_type") != NETWORK_CAPTURE_READY_EVENT_TYPE:
        raise InvalidAnalyzerMessage("unsupported_event_type")
    if raw.get("schema_version") != NETWORK_SCHEMA_VERSION:
        raise InvalidAnalyzerMessage("unsupported_schema_version")
    if message.topic() != NETWORK_CAPTURE_READY_TOPIC:
        raise InvalidAnalyzerMessage("topic_event_mismatch")
    try:
        event = deserialize_event(value, EventEnvelope[NetworkCaptureReadyV1])
    except EventDeserializationError:
        raise InvalidAnalyzerMessage("invalid_envelope_or_payload") from None
    key = message.key()
    if key is None or not isinstance(key, bytes):
        raise InvalidAnalyzerMessage("missing_or_invalid_key")
    try:
        decoded_key = key.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidAnalyzerMessage("invalid_utf8_key") from None
    if decoded_key != str(event.payload.application_id):
        raise InvalidAnalyzerMessage("application_key_mismatch")
    return event


class AnalyzerRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def load_and_validate(
        self, event: EventEnvelope[NetworkCaptureReadyV1]
    ) -> tuple[NetworkCapture, str]:
        row = self._session.execute(
            select(NetworkCapture, Application.package_name)
            .join(Application, Application.id == NetworkCapture.application_id)
            .where(NetworkCapture.id == event.payload.capture_id)
        ).one_or_none()
        if row is None:
            raise AnalyzerConsistencyError("capture does not exist")
        capture, package_name = row
        payload = event.payload
        if (
            capture.ready_event_id != event.event_id
            or capture.analysis_id != payload.analysis_id
            or capture.application_id != payload.application_id
            or package_name != payload.package_name
            or capture.scenario != payload.scenario
            or capture.object_key != payload.object_key
            or capture.capture_format != payload.capture_format
            or capture.capture_size_bytes != payload.capture_size_bytes
            or capture.transfer_file_size_bytes != payload.transfer_file_size_bytes
            or capture.expected_sha256 != payload.expected_sha256
        ):
            raise AnalyzerConsistencyError("capture-ready event does not match lifecycle state")
        return capture, cast(str, package_name)

    def claim_attempt(
        self,
        capture_id: object,
        *,
        attempt_started_at: datetime,
        stale_before: datetime,
    ) -> bool:
        """Atomically claim uploaded work or take over an abandoned analyzing attempt."""
        claimed = self._session.scalar(
            update(NetworkCapture)
            .where(
                NetworkCapture.id == capture_id,
                or_(
                    NetworkCapture.status == "uploaded",
                    and_(
                        NetworkCapture.status == "analyzing",
                        or_(
                            NetworkCapture.analysis_started_at.is_(None),
                            NetworkCapture.analysis_started_at <= stale_before,
                        ),
                    ),
                ),
            )
            .values(
                status="analyzing",
                analysis_attempt_count=NetworkCapture.analysis_attempt_count + 1,
                analysis_started_at=attempt_started_at,
                analysis_finished_at=None,
                error_code=None,
                error_message=None,
            )
            .returning(NetworkCapture.id)
        )
        return claimed is not None

    def release_attempt(self, capture_id: object, attempt_started_at: datetime) -> None:
        """Make a handled transient failure immediately eligible for another attempt."""
        self._session.execute(
            update(NetworkCapture)
            .where(
                NetworkCapture.id == capture_id,
                NetworkCapture.status == "analyzing",
                NetworkCapture.analysis_started_at == attempt_started_at,
            )
            .values(status="uploaded")
        )

    def mark_verified(self, capture_id: object, sha256: str, attempt_started_at: datetime) -> None:
        updated = self._session.scalar(
            update(NetworkCapture)
            .where(
                NetworkCapture.id == capture_id,
                NetworkCapture.status == "analyzing",
                NetworkCapture.analysis_started_at == attempt_started_at,
                NetworkCapture.expected_sha256 == sha256,
            )
            .values(verified_sha256=sha256)
            .returning(NetworkCapture.id)
        )
        if updated is None:
            raise AnalyzerConsistencyError("analyzer attempt no longer owns the capture")

    def mark_analyzed(self, capture_id: object, sha256: str, attempt_started_at: datetime) -> None:
        updated = self._session.scalar(
            update(NetworkCapture)
            .where(
                NetworkCapture.id == capture_id,
                NetworkCapture.status == "analyzing",
                NetworkCapture.analysis_started_at == attempt_started_at,
                NetworkCapture.expected_sha256 == sha256,
            )
            .values(
                verified_sha256=sha256,
                status="analyzed",
                analysis_finished_at=datetime.now(UTC),
                error_code=None,
                error_message=None,
            )
            .returning(NetworkCapture.id)
        )
        if updated is None:
            raise AnalyzerConsistencyError("analyzer attempt no longer owns the capture")

    def mark_failed(
        self,
        capture_id: object,
        error: TerminalAnalysisError,
        attempt_started_at: datetime,
    ) -> None:
        updated = self._session.scalar(
            update(NetworkCapture)
            .where(
                NetworkCapture.id == capture_id,
                NetworkCapture.status == "analyzing",
                NetworkCapture.analysis_started_at == attempt_started_at,
            )
            .values(
                status="failed",
                analysis_finished_at=datetime.now(UTC),
                error_code=error.code,
                error_message=str(error)[:1000],
            )
            .returning(NetworkCapture.id)
        )
        if updated is None:
            raise AnalyzerConsistencyError("analyzer attempt no longer owns the capture")


class NetworkAnalyzerWorker:
    def __init__(
        self,
        *,
        consumer: KafkaConsumer,
        session_factory: sessionmaker[Session],
        storage: ObjectStorage,
        publisher: NetworkPublisher,
        engine: AnalysisEngine,
        consumer_group: str,
        stale_analysis_seconds: int = 1800,
    ) -> None:
        self._consumer = consumer
        self._session_factory = session_factory
        self._storage = storage
        self._publisher = publisher
        self._engine = engine
        self._consumer_group = consumer_group
        self._stale_analysis_seconds = stale_analysis_seconds
        self._stopping = False
        self._closed = False

    def process_next(self, timeout: float = POLL_TIMEOUT_SECONDS) -> bool:
        message = self._consumer.poll(timeout=timeout)
        if message is None:
            return False
        context = self._message_context(message)
        try:
            event = decode_capture_ready(message)
        except InvalidAnalyzerMessage as error:
            logger.warning(
                "invalid network analyzer message skipped",
                extra={
                    **context,
                    "event": "network.analysis.message.skipped",
                    "reason": error.reason,
                },
            )
            self._consumer.commit(message)
            return True

        event_context = {
            **context,
            "event_id": str(event.event_id),
            "capture_id": str(event.payload.capture_id),
            "analysis_id": str(event.payload.analysis_id),
            "application_id": str(event.payload.application_id),
            "scenario": event.payload.scenario,
        }
        with self._session_factory.begin() as session:
            repository = AnalyzerRepository(session)
            capture, package_name = repository.load_and_validate(event)
            if capture.status == "analyzed":
                self._consumer.commit(message)
                logger.info(
                    "duplicate analyzed capture acknowledged",
                    extra={**event_context, "event": "network.analysis.duplicate"},
                )
                return True
            if capture.status in {"failed", "expired"}:
                self._consumer.commit(message)
                logger.warning(
                    "ineligible capture-ready event acknowledged",
                    extra={
                        **event_context,
                        "event": "network.analysis.message.skipped",
                        "status": capture.status,
                    },
                )
                return True
            capture_id = capture.id
            attempt_started_at = datetime.now(UTC)
            claimed = repository.claim_attempt(
                capture_id,
                attempt_started_at=attempt_started_at,
                stale_before=attempt_started_at - timedelta(seconds=self._stale_analysis_seconds),
            )
            if not claimed:
                raise AnalysisClaimUnavailable(
                    "capture is already owned by a fresh analyzer attempt"
                )

        logger.info(
            "network analysis started",
            extra={**event_context, "event": "network.analysis.started"},
        )
        try:
            with tempfile.TemporaryDirectory(prefix="sahabino-network-") as directory:
                suffix = ".pcapng" if event.payload.capture_format == "pcapng" else ".pcap"
                capture_path = Path(directory) / f"capture{suffix}"
                self._storage.download_file(event.payload.object_key, capture_path)
                metrics = analyze_capture(
                    capture_path,
                    event.payload.scenario,
                    event.payload.transfer_file_size_bytes,
                    declared_format=event.payload.capture_format,
                    expected_sha256=event.payload.expected_sha256,
                    engine=self._engine,
                )
            with self._session_factory.begin() as session:
                AnalyzerRepository(session).mark_verified(
                    capture_id, metrics.verified_sha256, attempt_started_at
                )
            payload = NetworkAnalysisCollectedV1(
                analysis_id=event.payload.analysis_id,
                capture_id=event.payload.capture_id,
                application_id=event.payload.application_id,
                package_name=package_name,
                scenario=event.payload.scenario,
                **metrics.model_dump(),
            )
            with self._session_factory() as session:
                publish_capture = session.get(NetworkCapture, capture_id)
                if publish_capture is None:
                    raise AnalyzerConsistencyError("capture disappeared before publication")
                self._publisher.publish_analysis(publish_capture, payload)
            with self._session_factory.begin() as session:
                AnalyzerRepository(session).mark_analyzed(
                    capture_id, metrics.verified_sha256, attempt_started_at
                )
        except (TerminalAnalysisError, CaptureObjectMissingError) as error:
            terminal_error = (
                error
                if isinstance(error, TerminalAnalysisError)
                else TerminalAnalysisError(error.code, str(error))
            )
            with self._session_factory.begin() as session:
                AnalyzerRepository(session).mark_failed(
                    capture_id, terminal_error, attempt_started_at
                )
            self._consumer.commit(message)
            logger.error(
                "network analysis failed",
                extra={
                    **event_context,
                    "event": "network.analysis.failed",
                    "error_code": terminal_error.code,
                },
            )
            return True
        except Exception:
            try:
                with self._session_factory.begin() as session:
                    AnalyzerRepository(session).release_attempt(capture_id, attempt_started_at)
            except Exception:
                logger.exception(
                    "could not release transient analyzer claim",
                    extra={
                        **event_context,
                        "event": "network.analysis.claim.release_failed",
                    },
                )
            logger.exception(
                "network analysis infrastructure failure",
                extra={
                    **event_context,
                    "event": "network.analysis.failed",
                    "failure_stage": "transient_infrastructure",
                },
            )
            raise

        self._consumer.commit(message)
        logger.info(
            "network analysis completed",
            extra={
                **event_context,
                "event": "network.analysis.completed",
                "status": "analyzed",
            },
        )
        return True

    def run_forever(self) -> None:
        try:
            while not self._stopping:
                self.process_next()
        finally:
            self.close()

    def stop(self) -> None:
        self._stopping = True

    def close(self) -> None:
        if self._closed:
            return
        self._consumer.close()
        self._closed = True

    def _message_context(self, message: Message) -> dict[str, object]:
        return {
            "topic": message.topic(),
            "partition": message.partition(),
            "offset": message.offset(),
            "consumer_group": self._consumer_group,
        }
