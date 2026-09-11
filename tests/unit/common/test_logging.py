from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest
from pydantic import SecretStr, ValidationError

from sahabino.common.config import Settings
from sahabino.common.observability import configure_logging


@pytest.fixture(autouse=True)
def reset_process_logging() -> None:
    yield
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        configured_logger = logging.getLogger(name)
        configured_logger.handlers.clear()
        configured_logger.setLevel(logging.NOTSET)
        configured_logger.propagate = True


def _configure(*, log_format: str = "json", level: str = "INFO") -> None:
    configure_logging(
        service_name="sahabino-test",
        level=level,  # type: ignore[arg-type]
        log_format=log_format,  # type: ignore[arg-type]
        environment="test",
    )


def test_json_output_contains_schema_and_extra_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()

    logging.getLogger("sahabino.test.worker").info(
        "crawl run completed",
        extra={"event": "crawler.run.completed", "crawl_run_id": "run-123"},
    )

    payload = json.loads(capsys.readouterr().out)
    assert datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00")).tzinfo
    assert payload == {
        **payload,
        "level": "INFO",
        "service": "sahabino-test",
        "environment": "test",
        "logger": "sahabino.test.worker",
        "message": "crawl run completed",
        "event": "crawler.run.completed",
        "crawl_run_id": "run-123",
    }


def test_json_output_preserves_exception_information(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()

    try:
        raise RuntimeError("unexpected failure")
    except RuntimeError:
        logging.getLogger("sahabino.test.worker").exception("worker failed")

    payload = json.loads(capsys.readouterr().out)
    assert "RuntimeError: unexpected failure" in payload["exc_info"]


def test_console_output_is_human_readable(capsys: pytest.CaptureFixture[str]) -> None:
    _configure(log_format="console")

    logging.getLogger("sahabino.test.worker").warning("retrying request")

    output = capsys.readouterr().out.strip()
    assert not output.startswith("{")
    assert "WARNING [sahabino-test] sahabino.test.worker: retrying request" in output


def test_configured_level_is_respected(capsys: pytest.CaptureFixture[str]) -> None:
    _configure(level="WARNING")
    logger = logging.getLogger("sahabino.test.worker")

    logger.info("hidden")
    logger.warning("visible")

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["message"] for record in records] == ["visible"]


def test_repeated_configuration_does_not_duplicate_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    _configure()

    logging.getLogger("sahabino.test.worker").info("one record")

    assert len(capsys.readouterr().out.splitlines()) == 1
    assert len(logging.getLogger().handlers) == 1


def test_uvicorn_loggers_use_the_central_handler_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).info("uvicorn event", extra={"event": "api.access"})

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["logger"] for record in records] == [
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
    ]


def test_secret_str_extra_uses_its_masked_representation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure()
    password = "do-not-log-this-password"

    logging.getLogger("sahabino.test.worker").info(
        "proxy authentication failed",
        extra={"proxy_password": SecretStr(password)},
    )

    output = capsys.readouterr().out
    assert password not in output
    assert "**********" in output


@pytest.mark.parametrize("field,value", [("log_level", "TRACE"), ("log_format", "xml")])
def test_settings_reject_invalid_logging_choices(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite://", **{field: value})  # type: ignore[arg-type]


def test_settings_reject_blank_environment() -> None:
    with pytest.raises(ValidationError, match="environment must not be blank"):
        Settings(database_url="sqlite://", environment="   ")
