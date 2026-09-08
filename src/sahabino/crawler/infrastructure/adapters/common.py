from __future__ import annotations

import re
from datetime import UTC, date, datetime

from sahabino.crawler.domain.errors import InvalidPackage, SchemaFailure

PACKAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")


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
