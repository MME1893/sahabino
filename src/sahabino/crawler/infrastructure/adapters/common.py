from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import ValidationError

from sahabino.crawler.domain.dto import ReviewDTO, ReviewsDTO
from sahabino.crawler.domain.errors import InvalidPackage, SchemaFailure

PACKAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")
MAX_INVALID_REVIEW_PERCENT = 5

_SAFE_REVIEW_FIELDS = frozenset(
    {
        "record",
        "external_review_id",
        "source_at",
        "author_name",
        "thumbs_up_count",
        "score",
        "content",
        "position",
        "observed_at",
        "source_adapter",
    }
)
_SAFE_DIAGNOSTIC_TOKEN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class InvalidReviewRecord:
    """A privacy-safe marker for one source item that could not be extracted."""

    field_names: tuple[str, ...]
    error_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ReviewIssue:
    source_position: int
    field_names: tuple[str, ...]
    error_types: tuple[str, ...]
    schema_failure: SchemaFailure | None = None


ReviewRecord = dict[str, Any] | InvalidReviewRecord
ReviewNormalizer = Callable[[dict[str, Any], int, datetime], ReviewDTO]


def validate_package(package_name: str) -> None:
    if not PACKAGE_PATTERN.fullmatch(package_name):
        raise InvalidPackage("package name is invalid")


def normalize_updated_on(value: object) -> date | None:
    if value is None or value == "Never updated":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise SchemaFailure("naive app update datetime is ambiguous")
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).date()
    if isinstance(value, str):
        for date_format in ("%b %d, %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, date_format).date()
            except ValueError:
                continue
        return None
    return None


def invalid_review_record(field_name: str, error: BaseException) -> InvalidReviewRecord:
    """Retain only allow-listed field metadata and the exception type, never source values."""
    return InvalidReviewRecord(
        field_names=(_safe_field_name(field_name),),
        error_types=(_safe_error_type(type(error).__name__),),
    )


def normalize_review_batch(
    records: list[ReviewRecord],
    *,
    package_name: str,
    adapter_name: str,
    requested_count: int,
    effective_limit: int,
    observed_at: datetime,
    normalize: ReviewNormalizer,
    logger: logging.Logger,
) -> ReviewsDTO:
    """Normalize independently and reject non-empty batches with over 5% invalid items.

    There is deliberately no small-batch exception: one bad item is tolerated only when the
    received batch has at least 20 records. Empty upstream collections remain valid, while a
    wholly invalid batch is always rejected.
    """
    considered = records[:effective_limit]
    accepted: list[ReviewDTO] = []
    issues: list[_ReviewIssue] = []

    for source_position, record in enumerate(considered, start=1):
        if isinstance(record, InvalidReviewRecord):
            issues.append(_ReviewIssue(source_position, record.field_names, record.error_types))
            continue
        if not isinstance(record, dict):
            issues.append(_ReviewIssue(source_position, ("record",), ("invalid_record_type",)))
            continue
        try:
            accepted.append(normalize(record, source_position, observed_at))
        except ValidationError as error:
            issues.append(_issue_from_validation_error(source_position, error))
        except SchemaFailure as error:
            issues.append(
                _ReviewIssue(
                    source_position,
                    ("source_at",),
                    (type(error).__name__,),
                    schema_failure=error,
                )
            )
        except (ValueError, OverflowError, OSError) as error:
            issues.append(_ReviewIssue(source_position, ("source_at",), (type(error).__name__,)))

    received_count = len(considered)
    skipped_count = len(issues)
    accepted_count = len(accepted)
    rejected = received_count > 0 and (
        accepted_count == 0 or skipped_count * 100 > received_count * MAX_INVALID_REVIEW_PERCENT
    )

    diagnostics = {
        "package_name": package_name,
        "adapter_name": adapter_name,
        "requested_count": requested_count,
        "received_count": received_count,
        "accepted_count": accepted_count,
        "skipped_count": skipped_count,
    }
    for issue in issues:
        logger.warning(
            "invalid review record skipped",
            extra={
                "event": "crawler.review.invalid_record",
                "source_position": issue.source_position,
                "field_names": issue.field_names,
                "validation_error_types": issue.error_types,
                **diagnostics,
            },
        )

    if rejected:
        logger.error(
            "review batch rejected by corruption threshold",
            extra={
                "event": "crawler.review.batch_rejected",
                "corruption_threshold_percent": MAX_INVALID_REVIEW_PERCENT,
                **diagnostics,
            },
        )
        if len(issues) == 1 and issues[0].schema_failure is not None:
            raise issues[0].schema_failure
        raise SchemaFailure("review batch exceeded the safe corruption threshold")

    return ReviewsDTO(reviews=tuple(accepted))


def _issue_from_validation_error(position: int, error: ValidationError) -> _ReviewIssue:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    fields = tuple(
        sorted(
            {
                _safe_field_name(str(detail["loc"][0]) if detail["loc"] else "record")
                for detail in details
            }
        )
    )
    error_types = tuple(
        sorted(
            {_safe_error_type(str(detail.get("type", "validation_error"))) for detail in details}
        )
    )
    return _ReviewIssue(position, fields or ("record",), error_types or ("validation_error",))


def _safe_field_name(field_name: str) -> str:
    return field_name if field_name in _SAFE_REVIEW_FIELDS else "record"


def _safe_error_type(error_type: str) -> str:
    return error_type if _SAFE_DIAGNOSTIC_TOKEN.fullmatch(error_type) else "validation_error"
