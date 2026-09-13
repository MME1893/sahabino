from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from sahabino.network.schemas import CaptureCreate


def _values() -> dict[str, object]:
    return {
        "application_id": uuid4(),
        "scenario": "upload",
        "filename": "capture.pcapng",
        "capture_size_bytes": 100,
        "transfer_file_size_bytes": 50,
        "sha256": "a" * 64,
    }


def test_capture_create_derives_validated_format_and_content_type() -> None:
    pcapng = CaptureCreate.model_validate(_values())
    pcap = CaptureCreate.model_validate({**_values(), "filename": "capture.PCAP"})

    assert pcapng.capture_format == "pcapng"
    assert pcapng.content_type == "application/x-pcapng"
    assert pcap.capture_format == "pcap"
    assert pcap.content_type == "application/vnd.tcpdump.pcap"


@pytest.mark.parametrize(
    "changes",
    [
        {"filename": "capture.zip"},
        {"capture_size_bytes": 0},
        {"transfer_file_size_bytes": -1},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"scenario": "stream"},
        {"unknown": True},
    ],
)
def test_capture_create_rejects_invalid_metadata(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CaptureCreate.model_validate({**_values(), **changes})
