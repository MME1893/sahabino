from __future__ import annotations

import sys
from typing import Any
from uuid import uuid4

import pytest

from sahabino.common.config import Settings
from sahabino.messaging import consumer as consumer_module
from sahabino.messaging.consumer import KafkaConsumer
from sahabino.network import __main__ as cli_module
from sahabino.network import bootstrap as bootstrap_module


def _settings(**changes: object) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        **changes,
    )


def test_analyzer_max_poll_interval_reaches_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class Engine:
        def __init__(self, **kwargs: object) -> None:
            calls["engine"] = kwargs

        def validate_runtime(self) -> None:
            calls["validated"] = True

    class Producer:
        @classmethod
        def from_settings(cls, settings: Settings) -> object:
            _ = settings
            return object()

    class Publisher:
        def __init__(self, producer: object) -> None:
            _ = producer

    class Consumer:
        @classmethod
        def from_settings(cls, settings: Settings, **kwargs: object) -> object:
            _ = settings
            calls["consumer"] = kwargs
            return object()

    class Worker:
        def __init__(self, **kwargs: object) -> None:
            calls["worker"] = kwargs

    monkeypatch.setattr(bootstrap_module, "TSharkEngine", Engine)
    monkeypatch.setattr(bootstrap_module, "KafkaProducer", Producer)
    monkeypatch.setattr(bootstrap_module, "KafkaNetworkPublisher", Publisher)
    monkeypatch.setattr(bootstrap_module, "KafkaConsumer", Consumer)
    monkeypatch.setattr(bootstrap_module, "NetworkAnalyzerWorker", Worker)
    monkeypatch.setattr(bootstrap_module, "create_sync_session_factory", lambda url: url)
    monkeypatch.setattr(bootstrap_module, "S3ObjectStorage", lambda settings: settings)
    settings = _settings(network_analyzer_max_poll_interval_ms=1_200_000)

    bootstrap_module.build_worker(settings)

    assert calls["validated"] is True
    assert calls["consumer"]["max_poll_interval_ms"] == 1_200_000
    assert calls["engine"]["max_parsed_records"] == settings.network_max_parsed_records


def test_analyzer_poll_interval_reaches_native_kafka_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NativeConsumer:
        instance: NativeConsumer

        def __init__(self, config: dict[str, Any]) -> None:
            self.config = config
            type(self).instance = self

        def subscribe(self, topics: list[str]) -> None:
            _ = topics

    monkeypatch.setattr(consumer_module, "Consumer", NativeConsumer)

    KafkaConsumer.from_settings(
        _settings(),
        "network-analyzer",
        ["network.capture.ready.v1"],
        max_poll_interval_ms=1_200_000,
    )

    assert NativeConsumer.instance.config["max.poll.interval.ms"] == 1_200_000


def test_tshark_validation_precedes_kafka_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Engine:
        def __init__(self, **kwargs: object) -> None:
            _ = kwargs

        def validate_runtime(self) -> None:
            raise RuntimeError("TShark executable was not found")

    monkeypatch.setattr(bootstrap_module, "TSharkEngine", Engine)
    monkeypatch.setattr(
        bootstrap_module.KafkaProducer,
        "from_settings",
        lambda settings: pytest.fail("Kafka must not be constructed"),
    )
    monkeypatch.setattr(
        bootstrap_module.KafkaConsumer,
        "from_settings",
        lambda settings, **kwargs: pytest.fail("Kafka must not be constructed"),
    )

    with pytest.raises(RuntimeError, match="executable was not found"):
        bootstrap_module.build_worker(_settings())


def test_cleanup_cli_does_not_construct_kafka(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture_id = uuid4()
    settings = _settings()
    monkeypatch.setattr(sys, "argv", ["sahabino.network", "cleanup", str(capture_id)])
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "S3ObjectStorage", lambda value: value)
    monkeypatch.setattr(cli_module, "create_sync_session_factory", lambda url: url)
    monkeypatch.setattr(cli_module, "cleanup_capture_object", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        cli_module.KafkaProducer,
        "from_settings",
        lambda value: pytest.fail("cleanup must not construct Kafka"),
    )

    cli_module.main()

    assert str(capture_id) in capsys.readouterr().out
