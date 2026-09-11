from __future__ import annotations

import logging
import logging.config
from datetime import UTC, datetime
from typing import Any, Literal

from pythonjsonlogger.json import JsonFormatter

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["console", "json"]

_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_LOG_FORMATS = frozenset({"console", "json"})


class _ServiceContextFilter(logging.Filter):
    def __init__(self, *, service_name: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def filter(self, record: logging.LogRecord) -> bool:
        record.__dict__["service"] = self._service_name
        record.__dict__["environment"] = self._environment
        return True


class _SahabinoJsonFormatter(JsonFormatter):
    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)

        base_fields = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "service": getattr(record, "service", "unknown"),
            "environment": getattr(record, "environment", "unknown"),
            "logger": record.name,
            "message": log_record.pop("message", record.getMessage()),
        }
        extra_fields = {key: value for key, value in log_record.items() if key not in base_fields}
        log_record.clear()
        log_record.update(base_fields)
        log_record.update(extra_fields)


def configure_logging(
    *,
    service_name: str,
    level: LogLevel,
    log_format: LogFormat,
    environment: str,
) -> None:
    """Configure process-wide stdout logging with shared service context."""
    if level not in _LOG_LEVELS:
        raise ValueError(f"unsupported log level: {level!r}")
    if log_format not in _LOG_FORMATS:
        raise ValueError(f"unsupported log format: {log_format!r}")
    if not service_name.strip():
        raise ValueError("service name must not be blank")
    if not environment.strip():
        raise ValueError("environment must not be blank")

    formatters: dict[str, dict[str, object]] = {
        "console": {
            "format": "%(asctime)s %(levelname)s [%(service)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
        },
        "json": {"()": _SahabinoJsonFormatter},
    }
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "service_context": {
                    "()": _ServiceContextFilter,
                    "service_name": service_name.strip(),
                    "environment": environment.strip(),
                }
            },
            "formatters": formatters,
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "level": level,
                    "formatter": log_format,
                    "filters": ["service_context"],
                }
            },
            "root": {"handlers": ["stdout"], "level": level},
            "loggers": {
                "uvicorn": {"handlers": [], "level": level, "propagate": True},
                "uvicorn.error": {"handlers": [], "level": level, "propagate": True},
                "uvicorn.access": {"handlers": [], "level": level, "propagate": True},
            },
        }
    )
